# Taifeng brand assets

The approved identity uses a navy kernel inside an open loop, with a teal capsule at the upper-right boundary. `taifeng-approved.png` preserves the exact user-approved ImageGen reference. The SVG assets are a clean geometric reconstruction for scalable use; their wordmark uses a system sans-serif rather than claiming an exact reproduction of the generated lettering.

## Files

| Asset | Use |
| --- | --- |
| `taifeng-lockup.svg` | Horizontal logo on light backgrounds |
| `taifeng-lockup-dark.svg` | White horizontal logo on dark backgrounds |
| `taifeng-mark.svg` | Standalone navy/teal symbol |
| `taifeng-mark-dark.svg` | Standalone white/teal symbol |
| `taifeng-avatar.png` | 512px avatar on a white tile |
| `taifeng-favicon-source.png` | 512px transparent icon; existing path retained |
| `favicon-{16,32,64}.png` | Small icons rendered from the SVG symbol |
| `taifeng-social.png` | 1280 × 640 GitHub social preview, ready for repository settings |
| `taifeng-social.svg` | Editable source for the social preview |
| `taifeng-approved.png` | Unmodified approved design reference |

## Meaning and naming

The kernel represents the small stable runtime core; the surrounding open loop represents continuous execution; the teal capsule suggests an extension crossing the runtime boundary. These are visual metaphors, not an architecture diagram.

The name comes from the mythic Taifeng described in the *Classic of Mountains and Seas*, as documented in [ADR 0001](../../decisions/0001-naming-taifeng.md). Its project metaphor is scheduling, guardianship, and driving invisible runtime flows. Do not replace this established origin with an invented interpretation of the individual characters 泰 and 逢.

## Use

- Navy: `#0B1424`; teal: `#00B8B3`.
- Preserve the core, open loop, and capsule as a single symbol; keep aspect ratios.
- Use the white variant on dark backgrounds. Do not place navy artwork on dark backgrounds.
- READMEs use a GitHub-compatible `picture` element to select light/dark variants.
- Repository social previews require a separate upload under GitHub repository settings. Committing the PNG does not configure that setting.
- These files do not change an organization or personal profile avatar.
