# Known-good Corstone-320 static object-detection baseline

This baseline freezes the four-image TensorFlow Lite Micro object-detection workload that booted on Corstone-320, initialized Ethos-U85 at 256 MACs, delegated the graph to a single `ethos-u` operator, detected faces in all four compiled images, and exited cleanly.

## Identity

- Baseline ID: `2026-09-25-static-u85-z256-49ca1073`
- Source HEAD: `49ca1073718f7f2cbb88610d5ca84fd47f2a4017`
- Source patch: `provenance/working-tree.patch`
- Firmware SHA-256: `3ff23c747e546c9c0047709e28995d5bfbab094d7867c583baa5bb6028d81ca2`
- Vela Z256 model SHA-256: `471424c3172ce07ab2250abbae4dc330ee8fd7a5d2955b9fba95aef027524fec`

The immutable local payload is stored outside normal Git tracking at:

```text
build-baselines/corstone-320/object-detection/2026-09-25-static-u85-z256-49ca1073/
```

Validate it from that directory with:

```zsh
shasum -a 256 -c ../../../../baselines/corstone-320/object-detection/2026-09-25-static-u85-z256-49ca1073/SHA256SUMS
```

`config/configure-command.txt` reconstructs the configure command from `CMakeCache.txt`; it is semantically equivalent but not claimed as the historically typed command. `config/build-command.txt` records the command validated for the final firmware.

## Console-log gap

The referenced `console-log-2026-09-25T21_01_57*` file was not present in the workspace and was not fabricated. The manifest records the benchmark figures supplied in chat as **user-reported and unverified** until the original log is attached and copied into the payload.

Do not rebuild into `build-cs320`; use a separate build directory for subsequent streaming work.