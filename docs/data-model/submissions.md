# Submissions

A **Submission** is the entry point into the system.  
It represents:

- The file being validated (JSON, XML, EnergyPlus IDF, etc.).
- The workflow _version_ to run.
- The organization, project, and user context.
- Metadata such as content type, size, and SHA-256 checksum.

Submissions can have multiple **Validation Runs** over time, but typically point to the _latest run_.
