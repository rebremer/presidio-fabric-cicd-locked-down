# Presidio Fabric Environment (fabric-cicd)

Deploys a Fabric **Environment** item named `Presidio` (analyzer + anonymizer)
into the `testpub` workspace, under the workspace folder `16_presidio`, using
[fabric-cicd 0.1.3](https://microsoft.github.io/fabric-cicd/0.1.3/).

## Layout

```
workspace/
  parameter.yml
  16_presidio/
    Presidio.Environment/
      .platform
      Setting/Sparkcompute.yml
      Libraries/
        requirements.txt          # public PyPI pkgs (presidio-*)
        environment.yml           # spaCy + en_core_web_lg model
        CustomLibraries/          # pre-downloaded wheels (DEP scenario)
deploy.py
parameter.yml -> workspace/parameter.yml
requirements-deploy.txt
```

## Step 1 — deploy from a connected (non-DEP) workspace first

Use this to validate the CICD pipeline end-to-end while pypi.org is reachable.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-deploy.txt

# Auth: az login   OR set AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET
$env:FABRIC_WORKSPACE_ID = "<guid-of-testpub-workspace>"
$env:FABRIC_ENVIRONMENT  = "PPE"
python deploy.py
```

## Step 2 — target the DEP-protected private workspace

`pypi.org` is unreachable from the DEP workspace, so wheels must be shipped
inside the Environment item itself:

1. On a build agent **with** internet access, pre-download every wheel:

   ```bash
   pip download \
     --dest workspace/16_presidio/Presidio.Environment/Libraries/CustomLibraries \
     --only-binary=:all: \
     --python-version 3.11 \
     --platform manylinux2014_x86_64 \
     presidio-analyzer==2.2.355 presidio-anonymizer==2.2.355 spacy==3.7.5

   curl -L -o workspace/16_presidio/Presidio.Environment/Libraries/CustomLibraries/en_core_web_lg-3.7.1-py3-none-any.whl \
     https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.7.1/en_core_web_lg-3.7.1-py3-none-any.whl
   ```

2. Remove (or empty) `Libraries/requirements.txt` and the `pip:` block in
   `Libraries/environment.yml`, so Fabric will **not** try to resolve from
   pypi.org during environment publish.

3. Commit the wheels and run `python deploy.py` from the Azure DevOps agent
   that has connectivity to the DEP workspace. fabric-cicd uploads the
   `CustomLibraries/` payload through the Fabric REST API (which is the
   only network hop required from the agent).

## Conflict mitigation

Presidio pulls in `spacy`, `pydantic`, `regex`, etc. which can clash with
versions baked into the Fabric runtime. Mitigations:

- Pin exact versions (already done above).
- Keep this dedicated `Presidio` environment and attach only the notebooks
  that need PII detection — do **not** make it the workspace default.
- If a clash surfaces at runtime, add the conflicting package to
  `CustomLibraries/` with a compatible version so the environment build
  resolves it deterministically instead of inheriting the runtime default.

## References

- fabric-cicd: <https://microsoft.github.io/fabric-cicd/0.1.3/>
- Presidio: <https://microsoft.github.io/presidio/>
- Fabric Environment items + custom libraries:
  <https://learn.microsoft.com/fabric/data-engineering/environment-manage-library>
