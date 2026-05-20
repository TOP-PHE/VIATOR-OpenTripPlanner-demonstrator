# VIATOR — visual identity brief

_Visual identity for VIATOR — a journey-planner demonstrator that checks results across timetable sources, built on OpenTripPlanner._

_**Powered by TrackOnPath SAS** — patrick.heuguet@trackonpath.com_
_**© 2026 TrackOnPath SAS. All rights reserved.**_
_Last updated: 2026-04-27_

---

## 1. The name

**VIATOR** — Latin for *traveller*. Used on Roman roadside inscriptions ("siste viator" — *stop, traveller*), it carries an instant association with **journey, movement, and a travelling person** in every European language. It is also short, four-syllable-friendly, easily pronounced in EN/FR/DE/IT/ES, and visually balanced as a wordmark.

Tagline:

> **VIATOR — a journey-planner demonstrator that checks results across timetable sources.**

---

## 2. The logo concept

### 2.1 Idea

A symbol that simultaneously reads as:

- the **letter V** of VIATOR,
- two **rails converging in perspective** toward a vanishing point (the bottom apex), and
- a **journey going forward** — the eye is pulled into the depth of the mark.

Three perpendicular **railway sleepers** cross the rails, shrinking as they approach the apex. The combination immediately reads as "rail" without resorting to a literal train silhouette.

### 2.2 Visual register

The mark follows a clean transit-iconography register:

- **Even-stroke line art** — no variable thickness.
- **Geometric construction** on a square grid.
- **Single colour** (TrackOnPath blue or black on white).
- **Reading correctly at small sizes** (down to favicon).
- **No decorative elements** — function over flourish.

The VIATOR mark respects all five.

### 2.3 Visual reference

```
       \         /
        \       /
   ──────────────────     ← sleeper 1
          \   /
       ─────────          ← sleeper 2
            V
           ───            ← sleeper 3
            *             ← vanishing point
```

(See `viator-icon.svg` for the production version and `viator-lockup.svg` for the horizontal lockup with wordmark.)

---

## 3. Colour palette

| Role | Name | HEX | Usage |
|---|---|---|---|
| Primary | TrackOnPath Blue | `#1C75BC` | Logo, headlines, primary buttons |
| Secondary | Rail Steel | `#5B6B82` | Body type on light backgrounds, secondary UI |
| Accent | Signal Amber | `#E89B2C` | Sparingly — alerts, "real-time" badges, focus states |
| Surface light | Paper | `#F5F7FB` | Page backgrounds, card surfaces |
| Surface dark | Tunnel | `#0E1A2D` | Dark-mode background |
| Neutral line | Sleeper | `#D4D9E2` | Borders, table rules |

The palette is deliberately monochrome-leaning. The accent amber is used **only** as a signal, never decoratively — same logic as railway signalling.

---

## 4. Typography

| Use | Typeface | Notes |
|---|---|---|
| Wordmark "VIATOR" | Geometric sans, **bold**, all-caps, **+6 letterspacing** | The lockup uses Helvetica Neue / Arial as a safe fallback. Production: substitute **Inter Display Bold** or **Eurostile Bold** for a more "transport" feel. |
| Headings | Inter (or any humanist sans) | Weights 600/700 |
| Body | Inter Regular | 400, 16 px base |
| Tabular / code | JetBrains Mono | For schedule timestamps, IDs, GraphQL queries |

The tagline in the lockup uses **+2.5 tracking** at small sizes for that "engineered", transit-caption feel.

---

## 5. Logo files

| File | Purpose |
|---|---|
| `viator-icon.svg` | Square icon, 64×64 viewBox. Use for favicon, app icon, dark-mode adaptations. |
| `viator-lockup.svg` | Horizontal lockup: icon + wordmark + tagline. Use for headers, slide deck title cards, README banners. |
| `trackonpath-logo.png` | TrackOnPath SAS company logo. Used in the upload-UI footer's "Powered by" line and any user-facing surface. Lifted from OSCAR — keep in sync if the master logo changes. |

The Dockerfile copies `branding/` into the runtime image; FastAPI mounts it at **`/static/branding/`**, so the upload UI references e.g. `<img src="/static/branding/trackonpath-logo.png">`.

Both are vector — scale freely. Both are single-colour `#1C75BC`; for dark-mode flip the stroke/fill to `#F5F7FB`.

### 5.1 Recommended exports (when needed)

```bash
# PNG raster exports (using rsvg-convert or Inkscape)
rsvg-convert -w 1024 -o viator-icon@1024.png  viator-icon.svg
rsvg-convert -w 512  -o viator-icon@512.png   viator-icon.svg
rsvg-convert -w 64   -o favicon.png           viator-icon.svg
rsvg-convert -w 2048 -o viator-lockup@2x.png  viator-lockup.svg
```

For favicon, additionally:

```bash
convert favicon.png -define icon:auto-resize=64,48,32,16 favicon.ico
```

---

## 6. Usage rules

**Do:**
- Keep clear space around the mark equal to the height of one sleeper (≈ 6 % of the icon height).
- Use only the colours in §3.
- Pair the icon with the wordmark in any horizontal context where there's room.
- Use the icon-only version for square placements (avatar, app tile, favicon).

**Don't:**
- Skew, rotate, or recolour the rails individually.
- Add gradients, drop-shadows, or 3D effects — VIATOR is a flat mark.
- Place on busy photographic backgrounds. Use a solid colour or a light tint.
- Re-letter "VIATOR" in a different style for special occasions.

---

## 7. Where it shows up in this stack

| Surface | What appears |
|---|---|
| Upload UI (`docker/web/app/templates/index.html`) | Lockup at top-left of the header |
| OTP debug client banner override | Icon-only at favicon scale |
| README and strategy doc headers | Lockup as embedded SVG |
| Slide decks for presentations | Lockup on cover, icon-only on subsequent pages |
| Future OJP API responses | None — APIs stay neutral |

If you want, I can wire the `viator-lockup.svg` into the upload UI header next.
