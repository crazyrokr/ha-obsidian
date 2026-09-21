# Plan: Implement Security Gates in CI/CD Pipeline

## Objective
Integrate the "Security Gates Summary Checklist for Automation" into the existing `/home/crazyrock/github/ha-obsidian/.github/workflows/cicd.yaml` pipeline.

## Current Pipeline Structure
The current pipeline uses a reusable workflow `crazyrokr/gha-workflows/.github/workflows/ha-addon-cicd.yaml@master` to build the application.

## Proposed Solution
We will add a new job `security-scan` to `cicd.yaml` that depends on the `run-builder` job, plus a pre-build dependency scan. This job will execute the security hurdles defined in the requirements, along with a pre-build dependency scan.

### Hurdles
1.  **Dependency Vulnerability Scan:** Scan project source/dependency files *before* building the image.
2.  **Authenticity:** Use `cosign verify` to validate the signature of the produced image.
3.  **Vulnerability Scanning:** Use `trivy` to scan the image for Critical/High CVEs.
4.  **Content Audit:** Use `syft` to generate an SBOM and `trivy` to analyze image configuration.

## Detailed Implementation Specification

### 0. Pre-build Dependency Scan
- Add a new job `dependency-scan` to `cicd.yaml` that runs before `run-builder`.
- Use `aquasecurity/trivy-action@0.24.0` with `scan-type: 'fs'` and `format: 'table'` to scan the project directory for vulnerabilities in dependency files (e.g., `package-lock.json`, `go.mod`, etc.).
- Configure to fail on `CRITICAL,HIGH` severity: `exit-code: '1'`.

### 1. Dynamic Image Referencing
- The `run-builder` job (reusable workflow) MUST be updated to output the built image tag (`IMAGE_REF`) to the job summary or output context so the `security-scan` job can consume it.
- `security-scan` will use `${{ needs.run-builder.outputs.image_ref }}`.

### 2. Job Definition (`cicd.yaml`)
```yaml
  dependency-scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Scan Dependencies
        uses: aquasecurity/trivy-action@0.24.0
        with:
          scan-type: 'fs'
          scan-ref: '.'
          format: 'table'
          exit-code: '1'
          severity: 'CRITICAL,HIGH'
          ignore-unfixed: true

  run-builder:
    needs: [auto-lock, dependency-scan]
    ...
  
  security-scan:
    needs: run-builder
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write # Required for keyless cosign/sigstore
      packages: read
    steps:
      - uses: actions/checkout@v4
      - name: Log in to Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
```

### 3. Granular Hurdle Configuration
- **Hurdle 2: Authenticity (`cosign`)**
  - Use `sigstore/cosign-installer@v3.5.0`.
  - For keyless verification: `cosign verify --certificate-identity-regexp '.*' --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' ${IMAGE_REF}`.
- **Hurdle 3: Vulnerability Scanning (`trivy`)**
  - Use `aquasecurity/trivy-action@0.24.0`.
  - Config: 
    ```yaml
    with:
      image-ref: ${{ needs.run-builder.outputs.image_ref }}
      format: 'table'
      exit-code: '1'
      severity: 'CRITICAL,HIGH'
      ignore-unfixed: true
    ```
- **Hurdle 4: Content Audit (`syft` + `trivy`)**
  - Use `anchore/sbom-action@v0.17.0` for `syft`.
  - Output: `format: cyclonedx-json`, `output-file: sbom.json`.
  - Config Analysis: `trivy conf ./obsidian/Dockerfile --exit-code 1 --severity CRITICAL,HIGH`.
  - Artifact Upload: `actions/upload-artifact@v4` for `sbom.json`.

## Verification & Testing
- Trigger the workflow via `workflow_dispatch` (manual test).
- Intentionally push an image that fails the scan to verify the pipeline halts correctly.
- Verify that successful builds pass all gates.

## Risks & Alternatives
- **Risk:** Increased build time.
- **Alternative:** Running security scans in parallel if independent of build output (not possible here as we scan the *produced* image).
