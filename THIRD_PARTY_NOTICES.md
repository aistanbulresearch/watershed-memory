# Data sources and attribution

Project code is licensed under MIT. The source datasets retain the terms provided by their publishers.

## Gallinas water-quality archive

Nichols et al. (2024), *Data archive: Longitudinal propagation of aquatic disturbances following the largest wildfire recorded in New Mexico, USA*. [Zenodo DOI](https://doi.org/10.5281/zenodo.12762324), pinned record **12764157**, version **1.0.1**, archive `rialgopi/HPCC-wildfire-v1.0.1.zip`.

License: [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/). Downloaded data remain unchanged. The project's extracted rows, unit conversions, timestamp interpretation and event summaries are derived work. The data are downloaded by the setup command rather than included in Git.

The [associated publication](https://doi.org/10.1038/s41467-024-51306-9) supplies scientific context. Its figures and text are not redistributed in this repository.

## USGS observations

Official observations from [USGS Water Services](https://waterservices.usgs.gov/) include station **08380500**, discharge parameter **00060** and precipitation parameter **00045**. The acquisition manifest records exact request URLs, retrieval details and SHA-256 checksums. Historical approved values may be updated by the provider; retain the source manifest with each replay.

## Software

The current data and persistence workflow uses the Python standard library. Source analysis programs from the downloaded archive are not executed or bundled. Strands integration is the next implementation milestone; dependency versions and notices will be added with that integration.
