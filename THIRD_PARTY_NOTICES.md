# Data sources and attribution

Project code is licensed under MIT. The source datasets retain the terms provided by their publishers.

## Gallinas water-quality archive

Nichols et al. (2024), *Data archive: Longitudinal propagation of aquatic disturbances following the largest wildfire recorded in New Mexico, USA*. [Zenodo DOI](https://doi.org/10.5281/zenodo.12762324), pinned record **12764157**, version **1.0.1**, archive `rialgopi/HPCC-wildfire-v1.0.1.zip`.

License: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). Downloaded source data remain unchanged. The project's extracted rows, unit conversions, timestamp interpretation and event summaries are derived work. A small three-window observation bundle is included in the application with an [adjacent attribution/change notice](watershed_memory/data/NOTICE.md). The full archive is downloaded only by the original proof's setup command.

The [associated publication](https://doi.org/10.1038/s41467-024-51306-9) supplies scientific context. Its figures and text are not redistributed in this repository.

## USGS observations

Official observations from [USGS Water Services](https://waterservices.usgs.gov/) include station **08380500**, discharge parameter **00060** and precipitation parameter **00045**. The acquisition manifest records exact request URLs, retrieval details and SHA-256 checksums. Historical approved values may be updated by the provider; retain the source manifest with each replay.

The offline field desk includes [52 saved USGS observations](watershed_memory/data/current-demo-observations.json) collected on September 12, 2026, with original observation times, retrieval receipts and semantic hashes. The bundled fixture does not fetch new readings. Its field assignment, operator report and verification are demonstration actions, labelled separately from the source observations.

## Software

The original data and persistence proof uses the Python standard library. The application installs its dependencies through `uv.lock`, including Strands Agents and the Bedrock AgentCore SDK (Apache-2.0). The optional AgentCore CodeZip bundles the locked runtime dependencies and retains their distributed license, notice and metadata files. Source analysis programs from the downloaded data archive are not executed or bundled.

AWS OpenTelemetry Distro 0.19.0 is Apache-2.0, authored by Amazon Web Services. Its wheel omits standalone license and notice files, so the Runtime artifact additionally includes [LICENSE](third_party/aws-otel-python-instrumentation/LICENSE), [NOTICE](third_party/aws-otel-python-instrumentation/NOTICE) and [third-party attribution](third_party/aws-otel-python-instrumentation/THIRD-PARTY-LICENSES) retrieved from the [upstream project](https://github.com/aws-observability/aws-otel-python-instrumentation) on September 12, 2026. The library is used unchanged.
