# Agent runbook: Alif E3 (Corstone-310) workloads on Corellium Atlas

This is a tool-agnostic runbook for building any use case under `source/app/use_case/` for the
Alif Ensemble E3 stand-in target (Corstone-310, Ethos-U65) and deploying/running it on a
Corellium Atlas `cs310` instance. It is written so any AI coding agent or IDE assistant — not
just Claude Code — can follow it directly from the repo. If you have Corellium MCP tools
available, exact tool-call names are given; if not, use the equivalent Corellium REST
API/CLI calls (see [support.avh.corellium.com](https://support.avh.corellium.com)) with the same
request shapes.

Read this whole file before acting — the packaging step (Step 2) has already failed twice in
practice for reasons that are not obvious from the CMake/build side at all.

## Background: what "Alif E3 workload" means here

This repo has no native HAL support for the Alif Ensemble **E3** part. E3 is 2x Cortex-M55 with
one 256 MAC/cycle + one 128 MAC/cycle Ethos-U55 — the closest throughput-class match among
targets this repo *does* support is Corstone-310 (`TARGET_PLATFORM=mps3`,
`TARGET_SUBSYSTEM=sse-310`), which is used as the stand-in.

**Build Corstone-310 with `ETHOS_U_NPU_ID=U65`, not `U55`.** This is intentional, not a typo:
Corstone-310's own reference design ships a Cortex-M85 + Ethos-U65, confirmed on real
hardware-in-the-loop (Corellium `cs310`) — a `U55` build boots but fails at `ethosu_dev_init()`'s
product-ID check (`dependencies/core-driver/src/ethosu_device_u55_u65.c`), because the NPU it
actually finds at `0x50004000` reports itself as U65. `U65` is the flag that boots; the console
then confirms `Arch: v1.1.0`, the same architecture generation as Ethos-U55 (U85, used by
Ensemble E4/E6/E8, is `Arch: v2.0.0` — a different generation). It is a throughput-class analogy,
not a cycle-accurate substitute for a real E3. Full rationale:
[`scripts/vsi/synthetic_video/README.md`](../vsi/synthetic_video/README.md).

This project's execution target is **Corellium Atlas** exclusively, not Arm's own FVP
binary/CLI. Don't invoke `FVP_Corstone_SSE-310 -C key=value...` as if it's what's running — it
isn't. Everything below talks to Corellium.

## Step 0: Prerequisites

- Submodules pulled (`git submodule update --init --recursive`).
- `resources_downloaded/<use_case>/` contains a Vela-compiled model for U65. Config ID for U65
  defaults to `Y256` (see `scripts/cmake/configuration_options/npu_opts.cmake`), so CMake expects
  a file named `<model>_vela_Y256.tflite`. For `object_detection` this already exists
  (`yolo-fastest_192_face_v4_vela_Y256.tflite`). For any other use case, generate it first:

  ```sh
  cd resources_downloaded/<use_case>
  vela --accelerator-config ethos-u65-256 --optimise Performance --memory-mode Dedicated_Sram \
    --output-dir . <model>.tflite
  mv <model>_vela.tflite <model>_vela_Y256.tflite
  ```

## Step 1: Configure and build the firmware

```sh
cmake -S . -B build-cs310-u65 \
  -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DTARGET_PLATFORM=mps3 \
  -DTARGET_SUBSYSTEM=sse-310 \
  -DETHOS_U_NPU_ID=U65 \
  -DUSE_CASE_BUILD=<use_case> \
  -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON
cmake --build build-cs310-u65 --target mlek_<use_case> --parallel 4
```

Replace `<use_case>` with the use case directory name under `source/app/use_case/` (e.g.
`object_detection`, `kws`, `img_class`). The resulting binary is
`build-cs310-u65/bin/mlek_<use_case>.axf`.

Only `object_detection` has been verified end-to-end against real Corellium `cs310` hardware
(see Step 4). Other use cases should follow the same mechanism but are unverified on this
target — don't report them as confirmed working until you've actually seen a real console log.

If you're re-running this against an existing `build-cs310-u65` directory, re-running the build
is cheap and safe — CMake only rebuilds objects whose inputs changed, so this is the right way
to confirm "is the already-deployed firmware current" before touching anything live.

## Step 2: Package the firmware for Corellium Atlas

**Do not upload the raw `.axf`/`.elf`.** Corellium's `fwpackage` image type for generic/IoT
flavors (`cs310`, `cs320`, ...) requires a **flat zip** (no wrapping directory, no `__MACOSX/`
junk) containing exactly two files:

- the firmware binary, renamed to exactly `firmware` (no extension)
- `Info.json` — JSON, **not** `info.plist` — with this schema:

  ```json
  {
      "UniqueIdentifier": "cs310",
      "Type": "iot",
      "DeviceIdentifier": "cs310",
      "Version": "1.0.0",
      "Build": "alif-u65-<use_case>"
  }
  ```

  `UniqueIdentifier`/`DeviceIdentifier` must match the Corellium flavor you're deploying to.
  `Build` is a free-form short tag — anything readable works. If this schema ever seems to drift
  or `create_image` rejects it, check for a newer known-good reference under `baselines/*/` first
  (e.g. `baselines/sse320_u85_u256/{firmware,Info.json,Archive.zip}` is a confirmed-working
  Corstone-320 example) rather than re-guessing.

Exact recipe:

```sh
BUILD_DIR=build-cs310-u65
USE_CASE=<use_case>
FLAVOR=cs310
PKG="$BUILD_DIR/corellium_pkg"

rm -rf "$PKG" && mkdir -p "$PKG"
cp "$BUILD_DIR/bin/mlek_${USE_CASE}.axf" "$PKG/firmware"
cat > "$PKG/Info.json" <<EOF
{
    "UniqueIdentifier": "${FLAVOR}",
    "Type": "iot",
    "DeviceIdentifier": "${FLAVOR}",
    "Version": "1.0.0",
    "Build": "alif-u65-${USE_CASE}"
}
EOF
( cd "$PKG" && zip -X "../mlek_${USE_CASE}-${FLAVOR}.zip" firmware Info.json )
```

Verify the zip is flat before uploading: `unzip -l "$BUILD_DIR/mlek_${USE_CASE}-${FLAVOR}.zip"`
should list exactly `firmware` and `Info.json`, nothing else.

## Step 3: Deploy to Corellium Atlas

Don't hardcode a project ID — discover the right project first (`list_projects`,
`list_instances`), or ask the user which project to use if more than one has existing `cs310`
instances. Then:

1. **Upload the package as an image:**
   `create_image(type="fwpackage", project_id=<project>, name=<descriptive name>, file_path=<zip path>)`
   → returns an image id. If this step itself fails with something like *"Unable to read file
   content for firmware info extraction"*, the manifest is wrong (wrong filename/format) — see
   Step 2.
2. **Create (or reuse) an instance:**
   `create_instance(project_id=<project>, name=<descriptive name>, flavor="cs310", os="1.0.0", fwpackage=<image id>)`.
   - Prefer creating a **new** instance rather than touching one that's already running and
     healthy, unless the user explicitly asks you to redeploy onto it (stopping/restarting a live
     instance interrupts whatever it's currently doing).
3. **Poll instance state** (`get_instance`) until it's `on` or `error`. `creating`/`booting` are
   transient; don't poll in a tight loop — wait a beat between checks.
4. **If `state: error`:** fetch console/instance error detail. `"Instance ... is missing a
   compute node"` means the fwpackage blob was accepted as a *file* but Corellium couldn't
   provision a machine from it — almost always a Step 2 packaging problem (raw binary instead of
   a package, non-flat zip, wrong manifest), not a flavor/quota/model issue. Fix the package and
   retry with a fresh `create_image`/`create_instance` rather than reusing the broken instance.
5. **Clean up** any broken instances/images from failed attempts (`delete_instance`,
   `delete_image`) instead of leaving clutter behind.

## Step 4: Verify — read the real console, don't assume

`get_console(instance_id)` on a healthy boot of this workload shows, in order:

```
INFO - Initialising Ethos-U device@0x50004000
INFO - Ethos-U device initialised
INFO - Ethos-U version info:
INFO - 	Arch:       v1.1.0
INFO - 	MACs/cc:    256
INFO - Target system design: Arm Corstone-310 - AN555
...
INFO - Final results:
INFO - Total number of inferences: 1
INFO - 0) (0.999246) -> Detection box: {x=89,y=17,w=41,h=56}
...
INFO - Main loop terminated successfully.
```

(The exact detection boxes/confidences and tensor shapes are specific to `object_detection`'s
sample images and model; other use cases will print their own equivalent results.)

**Only report numbers/log lines you actually read back from a live `get_console` call.** Never
fabricate or extrapolate console output, cycle counts, or confidence values — if something isn't
captured, say so explicitly rather than inventing a plausible-looking value.

## Common failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Boots but faults/hangs at `ethosu_dev_init()`'s product-ID check | Built with `ETHOS_U_NPU_ID=U55` | Use `U65` — see Background above |
| `create_image` fails: *"Unable to read file content for firmware info extraction"* | Manifest is missing, wrong format (e.g. `info.plist`), or wrong filename | Use `Info.json` (JSON) with the schema in Step 2 |
| `create_instance` → `state: error`; console/error says *"missing a compute node"* | Uploaded a raw `.axf`/`.elf`, or zip isn't flat / missing `Info.json` | Rebuild the zip exactly as in Step 2; re-upload as a fresh image |
| Camera/video input never arrives (VSI-based build) | Target has no VSI peripheral or guest-visible NIC on Corellium | Use the vMMIO ethernet-camera path (`scripts/ethernet_camera/`) instead of `scripts/vsi/`, or stick to static sample images |
| About to invoke `FVP_Corstone_SSE-310 -C ...` | Wrong execution target for this project | This project runs on Corellium Atlas, not Arm's own FVP binary |

## Example prompts

Any of these, said to an AI agent working in this repo, should be enough to kick off this flow
end to end:

- *"Compile the Alif E3 workload (object_detection) for Corstone-310 and run it on a new
  Corellium Atlas cs310 instance. Follow scripts/corellium/AGENT.md."*
- *"Build the kws use case for the Alif E3 / Corstone-310 target (U65) and deploy it to Corellium
  Atlas as a new cs310 instance named kws-u65-demo."*
- *"Rebuild build-cs310-u65 and tell me whether the firmware running on `<instance name>` is
  already up to date, without restarting anything."*
- *"Just build the Corstone-310/E3 object_detection firmware — don't touch Corellium yet."*
- *"My Corellium cs310 instance is stuck in state=error after uploading firmware. Diagnose it
  using scripts/corellium/AGENT.md's failure-mode table."*
