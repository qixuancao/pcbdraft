# PCBDraft brand assets

`pcbdraft-mark.svg` is the deterministic master mark for the first PCBDraft
release. Its composition was selected from a built-in image-generation concept,
then reconstructed as flat vector geometry so small icons have clean edges and
exact colors. The generated 1254×1254 RGBA source PNG is preserved by digest:

```text
SHA-256 abbe8e65746ad62ce7a70e4a81200f1a6f2ec17d56894bc513b41d1466a3b60a
```

The mark turns a terminal prompt chevron into three copper PCB traces on a dark
rounded board. The repository also carries deterministic 32, 64, 128, 256, and
512 px icons and a 1280×640 GitHub social preview.

Regenerate or verify every derivative from the repository root:

```bash
scripts/generate-brand-assets.sh
scripts/generate-brand-assets.sh --check
```

The script requires ffmpeg and the pinned system font path stated in the script.
It checks the source digest, strips metadata, uses a single PNG encoder thread,
stages all output privately, and updates only changed files.
