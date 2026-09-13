"""Optional local operator routes behind the application's existing HTTP boundary."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .case_types import HumanAction, WorkflowConflict, _identifier
from .desk import CurrentDesk
from .fact_validation import timestamp
from .field_http_types import FieldResponseBody


class ResponseBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: StrictStr = Field(min_length=1, max_length=128)
    task_id: StrictStr = Field(min_length=1, max_length=128)
    expected_revision: StrictInt = Field(ge=1, lt=2**63)
    action: Literal["APPROVE", "MODIFY", "DEFER", "DISMISS", "CANCEL"]
    note: StrictStr = Field(default="", max_length=1000)
    title: StrictStr | None = Field(default=None, min_length=1, max_length=120)
    next_check_at: StrictStr | None = Field(default=None, min_length=20, max_length=40)


def _now():
    return datetime.now(timezone.utc)


def router(desk: CurrentDesk) -> APIRouter:
    routes = APIRouter()

    @routes.get("/current", include_in_schema=False)
    def current_page():
        return FileResponse(Path(__file__).resolve().parent.parent / "static" / "current.html")

    @routes.get("/api/current/case")
    def case():
        try:
            return desk.snapshot(now=_now())
        except Exception as error:
            raise HTTPException(
                503, "The saved case is temporarily unavailable. Refresh shortly."
            ) from error

    @routes.post("/api/current/responses")
    def respond(body: ResponseBody):
        try:
            action = HumanAction(
                body.task_id,
                body.expected_revision,
                body.action,
                body.note,
                body.title,
                None if body.next_check_at is None else timestamp(body.next_check_at),
            )
            return desk.respond(action, request_id=body.request_id, now=_now())
        except WorkflowConflict as error:
            raise HTTPException(
                409, "The saved plan has changed. Refresh and review it before responding."
            ) from error
        except (ValueError, KeyError) as error:
            raise HTTPException(
                400, "Check the selected action, plan and future check time."
            ) from error
        except Exception as error:
            raise HTTPException(
                503, "The response could not be confirmed. Retry this same saved response."
            ) from error

    @routes.get("/api/current/assessments/{attempt_id}")
    def assessment(attempt_id: str):
        try:
            return desk.assessment(attempt_id)
        except KeyError as error:
            raise HTTPException(404, "This saved assessment was not found in this case.") from error
        except Exception as error:
            raise HTTPException(503, "This assessment is temporarily unavailable.") from error

    @routes.get("/api/current/field-work/{plan_id}")
    def field_work(plan_id: str):
        try:
            _identifier(plan_id, "plan_id")
        except ValueError as error:
            raise HTTPException(400, "Check the selected field work identifier.") from error
        try:
            if desk.fields is None:
                raise KeyError("field work is not configured")
            return desk.field_work(plan_id, now=_now())
        except KeyError as error:
            raise HTTPException(404, "This field work was not found in this case.") from error
        except Exception as error:
            raise HTTPException(503, "This field work is temporarily unavailable.") from error

    @routes.post("/api/current/field-responses")
    def field_response(body: FieldResponseBody):
        try:
            if desk.fields is None or desk.principal is None:
                raise ValueError("field responses are not configured")
            simulated = desk.cases.context(desk.case_id)["simulated"]
            command = body.command.to_command(simulated=simulated)
            return desk.respond_field(
                command,
                request_id=body.request_id,
                expected_case_revision=body.expected_case_revision,
                now=_now(),
            )
        except WorkflowConflict as error:
            raise HTTPException(
                409, "The saved field work has changed. Refresh and review it before responding."
            ) from error
        except (ValueError, KeyError) as error:
            raise HTTPException(
                400, "Check the selected field action, work revision and evidence."
            ) from error
        except Exception as error:
            raise HTTPException(
                503, "The field response could not be confirmed. Retry this same saved response."
            ) from error

    return routes
