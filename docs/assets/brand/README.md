# CopperWright brand assets

`copperwright-mark-v1-source.png` is the reviewed GPT Image 2 source mark supplied
for the first CopperWright release. It is preserved byte-for-byte at 1254×1254 RGB:

```text
SHA-256 eb9a60f013b0e9413ee58442779884e2fed67f8080b105038367412b406b4004
```

The source depicts a copper PCB-trace “W” on a dark square field. Do not overwrite
it with a derivative. The repository also carries deterministic 32, 64, 128, 256,
and 512 px icons and a 1280×640 GitHub social preview.

Regenerate or verify every derivative from the repository root:

```bash
scripts/generate-brand-assets.sh
scripts/generate-brand-assets.sh --check
```

The script requires ffmpeg and the pinned system font path stated in the script.
It checks the source digest, strips metadata, uses a single PNG encoder thread,
stages all output privately, and updates only changed files.
