# Hosted Voice Agent Test Guide

This guide validates the preview `azd` Hosted Voice Agent experience against an
existing Foundry project. It covers the same lifecycle surfaces used by hosted
code agents and `invocations_ws` agents, plus the Voice wrapper and local Voice
dashboard.

## Architecture under test

```text
Voice client / local dashboard
        |
        | Voice realtime protocol
        v
Voice wrapper (kind=voice, model_type=hosted_agent)
        |
        | Voice Bridge Protocol 1.0 over invocations_ws/1.0.0
        v
Hosted target (kind=hosted, user code)
```

Users write and deploy the hosted target code. The wrapper owns VAD, STT, TTS,
output voice, and Voice session settings. The wrapper and target must be in the
same Foundry project.

## Prerequisites

- A Foundry project in a region where Hosted Voice is enabled. West US 2 is the
  recommended preview validation region.
- A model deployment that the hosted target can invoke.
- `az login` and `azd auth login` completed for the test subscription.
- A local build of the PR extension installed:

  ```powershell
  cd cli/azd/extensions/azure.ai.agents
  azd x build
  ```

- A Hosted Voice target implementing Voice Bridge Protocol 1.0. The current
  .NET sample is under `samples/voice-hosted-agent/voice-hosted-agent-dotnet` in
  the `voice-first-agent-dev` repository.

## Manifest

Generate the composition interactively from compatible source code:

```powershell
$env:AZD_AI_AGENT_ENABLE_PROMPT_VOICE = "true"
azd ai agent init
```

Select **Create a hosted voice agent from the code in the current directory**.
For CI, use `--kind hosted-voice` with the normal code deploy flags and
`--no-prompt`.

Use one project service, one hosted target, and one Voice wrapper. The wrapper
references the target by its `azure.yaml` service name, not by a remote agent
name copied into the file.

```yaml
services:
  ai-project:
    host: azure.ai.project

  voice-target:
    host: azure.ai.agent
    project: ./src/voice-target
    language: csharp
    kind: hosted
    name: voice-target
    uses:
      - ai-project
    metadata:
      voiceLiveCompatible: "true"
      bridgeProtocolVersion: "1.0"
    protocols:
      - protocol: invocations_ws
        version: 1.0.0
    codeConfiguration:
      runtime: dotnet_10
      entryPoint: VoiceHostedAgent.dll
      dependencyResolution: bundled
    container:
      resources:
        cpu: "1"
        memory: 2Gi

  voice:
    host: azure.ai.agent
    kind: prompt-voice
    name: voice
    uses:
      - ai-project
      - voice-target
    modelType: hosted_agent
    targetAgent:
      service: voice-target
      version: deployed
    store: false
    audio:
      output:
        voice:
          type: azure_standard
          name: en-US-JennyNeural
```

`version: deployed` pins the wrapper to the target version recorded by the
current azd environment. Floating latest is intentionally not supported.

## Automated local checks

Run from `cli/azd/extensions/azure.ai.agents`:

```powershell
go test ./...
go vet ./...
azd x build
```

Expected: all commands succeed.

## Package and publish

Run from the test azd project:

```powershell
azd package --all
azd publish --all
```

Expected:

- the hosted target builds and produces a code ZIP or container artifact;
- the project and Voice wrapper report no package artifact;
- publish succeeds without attempting to publish wrapper code.

This matches the existing split between a code agent service and a declarative
service resource.

## Deploy

```powershell
azd deploy --all --no-prompt
```

Expected ordering:

1. project dependency is ready;
2. target is packaged and deployed;
3. target version becomes active;
4. wrapper validates the target;
5. wrapper is created or updated through the unified Voice API.

Expected target validation:

- same Foundry project;
- `kind=hosted`;
- status `active`;
- `invocations_ws/1.0.0`;
- metadata `voiceLiveCompatible=true`;
- metadata `bridgeProtocolVersion=1.0`.

Expected output includes the target `invocations_ws` endpoint and wrapper Voice
endpoint.

## Environment outputs

```powershell
azd env get-values
```

Expected target values:

```text
AGENT_<TARGET>_NAME
AGENT_<TARGET>_VERSION
AGENT_<TARGET>_PROJECT_ENDPOINT
AGENT_<TARGET>_INVOCATIONS_WS_ENDPOINT
```

Expected wrapper values:

```text
AGENT_<WRAPPER>_NAME
AGENT_<WRAPPER>_VERSION
AGENT_<WRAPPER>_PROJECT_ENDPOINT
AGENT_<WRAPPER>_ENDPOINT
AGENT_<WRAPPER>_TARGET_NAME
AGENT_<WRAPPER>_TARGET_VERSION
```

The wrapper endpoint is the end-user Voice realtime endpoint. The target
`invocations_ws` endpoint is a diagnostic/developer endpoint.

## Show and doctor

```powershell
azd ai agent show voice-target --output json
azd ai agent show voice --output json
azd ai agent doctor --output json
```

Expected:

- target show returns an active hosted definition and `invocations_ws` endpoint;
- wrapper show returns `kind=voice`, `model_type=hosted_agent`, and the pinned
  target name/version;
- doctor reports no failed checks.

`doctor` is project-wide and does not currently accept a service argument.

## Repeat and independent deployment

Run the wrapper deployment twice:

```powershell
azd deploy voice --no-prompt
azd deploy voice --no-prompt
```

Expected: the first command creates or updates the wrapper and the second uses
the unified update path. Both preserve a working endpoint.

The wrapper can be managed independently after its target is deployed:

```powershell
azd ai agent delete voice --force --no-prompt
azd deploy voice --no-prompt
```

Expected:

- delete removes only the wrapper and clears its environment markers;
- the target remains active;
- the single-service deploy recreates only the wrapper.

Do not delete the target before its managed wrapper. Full reverse-order
cleanup and ownership-aware `azd down` behavior are planned follow-up work.

## Direct target protocol smoke

Use a Voice Bridge Protocol client against:

```text
AGENT_<TARGET>_INVOCATIONS_WS_ENDPOINT
```

Expected frame sequence includes `session.ready`, response text events, and
`response.done`. This isolates target code and model access from the Voice
wrapper. It is diagnostic only and is not the end-user path.

Generic `azd ai agent invoke` intentionally rejects an `invocations_ws`-only
target because the command supports `responses`, `invocations`, and `a2a`.

## Local Voice dashboard

From the `voice-first-agent-dev` repository:

```powershell
cd tests/voice-agent-tests/voice-agents-tests-dashboard/voice_demo
python -m pip install -r requirements.txt
python demo_server.py `
  --backend "<account>/<project>" `
  --bind 127.0.0.1 `
  --port 9527 `
  --auto-delete false
```

Open `http://127.0.0.1:9527` on the same machine.

Confirm:

1. Agent backend is the intended Foundry project.
2. Inference modes include **Hosted agent**.
3. Agent discovery includes both the hosted target and Voice wrapper.
4. The connection dropdown selects the wrapper, not the target. This is by
   design: Voice clients connect to the wrapper.
5. The wrapper details show the expected target name/version.
6. Connect and send a typed turn; text and audio should return.
7. With a microphone available, send a spoken turn and verify STT, target text,
   and output audio.

If the UI reports `getUserMedia ... Requested device not found`, the remote
desktop/browser has no microphone. This does not indicate an agent failure.

## Negative tests

Run these against a disposable environment and restore every value after the
test.

| Case | Expected failure |
|---|---|
| Explicit `AZURE_VOICE_AGENT_API=legacy` | Hosted wrapper requires unified-flat |
| Target version marker points to a missing version | Remote target validation fails before wrapper mutation |
| Target project marker differs from the wrapper project | Dependency validation rejects cross-project binding |
| Target status is not active | Compatibility validation rejects it |
| Target lacks `invocations_ws/1.0.0` | Compatibility validation rejects it |
| Target lacks Voice Bridge metadata | Compatibility validation rejects it |
| Wrapper includes model/instructions/tools/handoff | Manifest validation rejects target-owned fields |
| Wrapper does not list target under `uses` | Target service resolution fails |

After negative testing, run a normal wrapper deploy and one text turn to verify
the environment was restored.

## Experience alignment matrix

| Capability | Hosted/code agent | `invocations_ws` agent | Hosted Voice in this PR |
|---|---|---|---|
| `azure.ai.agent` service | Yes | Yes | Yes, target and wrapper |
| Project reuse/provision | Yes | Yes | Unchanged |
| Package/publish target code | Yes | Yes | Unchanged |
| Service graph deployment | Yes | Yes | Yes, `uses` orders target then wrapper |
| Single-service deploy | Yes | Yes | Yes |
| Remote version pinning | Yes | Yes | Wrapper pins deployed target version |
| `show` | Yes | Yes | Yes for both layers |
| `doctor` | Yes | Yes | Project-wide checks pass |
| Generic `invoke` | Responses/invocations | Not for WS | Not for Voice/WS; use Voice client/UI |
| Delete agent | Yes | Yes | Yes for each layer; wrapper first |
| `azd down` ownership cleanup | Existing behavior | Existing behavior | Follow-up for wrapper reverse cleanup |
| `azd ai agent init` scaffold | Yes | Yes | Yes, interactive and no-prompt CI paths |

## Evidence to record

For manual sign-off, record:

- date/time and tester;
- PR commit and installed extension version;
- subscription, region, account, project, and endpoint;
- target and wrapper names/versions;
- output of test/build/package/publish/deploy/show/doctor;
- dashboard screenshot showing Hosted mode and wrapper target binding;
- typed response text;
- spoken input transcript and number/presence of returned audio frames;
- negative test results;
- cleanup status.
