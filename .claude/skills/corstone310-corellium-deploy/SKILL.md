---
name: corstone310-corellium-deploy
description: Build an mlek use case for the Alif Ensemble E3 stand-in target (Corstone-310, mps3/sse-310, Ethos-U65) and deploy/run it on a Corellium Atlas cs310 instance, verifying via real console output. Trigger on requests to compile/build/run/deploy an "Alif E3" workload, or any "Corstone-310" + "Corellium"/"Atlas" build-and-run request in this repo.
---

# Build & run an Alif E3 (Corstone-310) workload on Corellium Atlas

Full rationale, troubleshooting table, and the authoritative version of every step live in
[`scripts/corellium/AGENT.md`](../../../scripts/corellium/AGENT.md) — read that file now if you
haven't already; this skill is a condensed, action-oriented checklist on top of it, written for
execution inside Claude Code specifically (tool-call names included).

## The two facts that matter most

1. **Build Corstone-310 with `ETHOS_U_NPU_ID=U65`, never `U55`.** `U55` boots but then fails
   Ethos-U's product-ID check on real Corstone-310/`cs310` hardware — the NPU actually present
   reports itself as U65. This is the documented stand-in for Alif Ensemble E3 (no E3 HAL exists
   in this repo). Don't "fix" this to U55.
2. **Corellium firmware uploads for `cs310` must be a flat zip containing `firmware` (the
   renamed binary) + `Info.json` (JSON, not a plist).** A raw `.axf` upload creates an instance
   that lands in `state: error` with a "missing a compute node" console error. A zip with a
   guessed/wrong manifest format makes `create_image` itself reject the file. Match the schema
   in Step 2 below exactly, or copy it fresh from `baselines/sse320_u85_u256/Info.json`.

## Steps

1. **Build** (swap `<use_case>` for the target use case, e.g. `object_detection`):

   ```sh
   cmake -S . -B build-cs310-u65 \
     -DCMAKE_TOOLCHAIN_FILE=scripts/cmake/toolchains/bare-metal-gcc.cmake \
     -DCMAKE_BUILD_TYPE=Release \
     -DTARGET_PLATFORM=mps3 -DTARGET_SUBSYSTEM=sse-310 -DETHOS_U_NPU_ID=U65 \
     -DUSE_CASE_BUILD=<use_case> -DCMSIS_DSP_MIN_REQ_SRC_LIST=ON
   cmake --build build-cs310-u65 --target mlek_<use_case> --parallel 4
   ```

   If `build-cs310-u65` already exists, just re-run the `cmake --build` line — if nothing
   recompiles, the already-deployed firmware (if any) is already current; say so instead of
   redeploying unnecessarily.

2. **Package** for Corellium (see AGENT.md Step 2 for the full recipe):

   ```sh
   BUILD_DIR=build-cs310-u65; USE_CASE=<use_case>; FLAVOR=cs310
   PKG="$BUILD_DIR/corellium_pkg"; rm -rf "$PKG" && mkdir -p "$PKG"
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

3. **Deploy**, using the Corellium MCP tools if present in this session:
   - `mcp__corellium__list_projects` / `list_instances` — pick the right project; don't hardcode
     one. Ask the user if more than one plausible project exists.
   - `mcp__corellium__create_image` with `type="fwpackage"`, `file_path` pointing at the zip from
     Step 2.
   - `mcp__corellium__create_instance` with `flavor="cs310"`, `os="1.0.0"`, `fwpackage=<image id>`.
     Prefer a **new** instance over restarting an already-running one unless told otherwise —
     check `list_instances` for anything already serving this workload before assuming you need
     a fresh one.
   - `mcp__corellium__get_instance` to poll state until `on`/`error` (don't tight-loop; a short
     wait between checks is fine).
   - On `state: error`, `mcp__corellium__get_console` for the error detail — see AGENT.md's
     failure-mode table before trying anything else.
   - `mcp__corellium__delete_instance` / `delete_image` to clean up broken attempts.

   If the Corellium MCP tools aren't available in this environment, use the equivalent Corellium
   REST API/CLI calls with the same request shapes (image upload → instance create referencing
   that image).

4. **Verify** with `mcp__corellium__get_console` (or equivalent). Expect `Ethos-U device
   initialised`, `Arch: v1.1.0`, `Target system design: Arm Corstone-310 - AN555`, then real
   inference output. Report only what the console actually shows — never fabricate log lines,
   cycle counts, or confidence values; if a figure can't be captured, say so explicitly.

## Example prompts

- *"Compile the Alif E3 workload (object_detection) for Corstone-310 and run it on a new
  Corellium Atlas cs310 instance."*
- *"Build the kws use case for the Alif E3 / Corstone-310 target and deploy it to Corellium Atlas
  as a new instance named kws-u65-demo."*
- *"Check whether the firmware on `<instance name>` is already up to date with the current
  source tree, without restarting it."*
- *"Just build the Corstone-310/E3 object_detection firmware — don't touch Corellium yet."*
- *"My Corellium cs310 instance is stuck in state=error after uploading firmware — diagnose it."*
