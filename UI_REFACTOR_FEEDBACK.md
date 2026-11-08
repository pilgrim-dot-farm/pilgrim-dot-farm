# UI Feedback

## Mobile
* Play/stop button diameter should be reduced 4 px (collides with top/bottom)
* Strange behavior showing and then hiding the keyboard (extra height added to bottom, some purple some white)
    * Manifests in Chrome
    * See IMG_6224.PNG (then delete it)
    * Safari corrrectly has our page background!
* Chat should be scrolled down upon enter
* Ratings are sometimes incorrectly greyed out when a song is playing
* GRAZE UPON IT is out of place — no need for the element
* We can safely add back "Farm to table slop — We're keeping the house warm." (in proportion)
* Bottom button row:
    * The cyan highlight on the top looks good
    * For highlighted item:
        * Slightly bigger font
        * Cyan on the sides same thickness as up top

## Desktop
* New page starts at the top, but nav pane seems to start at REQUEST LINE
* At what height does the chat begin having scroll bar?

## Both Mobile and Desktop
* Sub-headings are inconsistent:
    * Request line has pink all caps with underline
    * About section has white with a purple shaded box
    * Make all sub-headings like the about section
    * Make all sub-headings centered
* We don't want clicking or tapping on an image to open it

# Extra Request
* We want our cornucopia for paid requests to sparkle
* We have Sparkling Cornucopia Harvest Animation.gif
* What to do:
    1. Ensure the background is truly transparent
    2. Ensure proper cropping
    3. Replace the static cornucopia image with our gif
## Disposition (2026-11-08)

Shipped in the follow-up commit after `9eb706d`:

- **Mobile** — mini PLAY/STOP is 40 px (was 44); the iOS-Chrome keyboard gap
  is dark because `html` now carries `--bg` (the shell still follows `--vvh`);
  entering CHAT snaps the log to the newest message; the SHEEP PEN status
  strip ("GRAZE UPON IT") is gone on phones (the element stays in the DOM,
  hidden, so `chat.js` is untouched); the tagline is back in proportion under
  the one-line logo; the selected tab is 13 px with 2 px cyan left/right
  borders matching the 2 px top border.
- **Queue** — 20 rows per page with a `1–20 of N` prev/next pager (hidden when
  the whole queue fits), so the sparkling cornucopia only ever animates in 20
  rows at a time.
- **Desktop** — a fresh page always highlights RADIO (at `scrollTop ≈ 0` the
  tall-viewport 35 % rule no longer picks REQUEST LINE). Chat scrollbar
  answer: `.chat-log` is `min-height: 180px; max-height: 42vh`, so it scrolls
  once messages exceed 42 % of the window height (302 px at 720 px tall).
- **Both** — every sub-heading now uses the ABOUT treatment (shaded gradient
  box, white, centred): the request line's GROUND RULES / FREE FOR ALL /
  PRIORITY REQUEST / NOW QUEUED / AIRED REQUESTS included. The motivation and
  infra images are no longer links (tap does nothing).
- **Extra** — the static cornucopia is replaced by
  `web/img/cornucopia.webp`: the source GIF cropped to its content
  (472×400 at 20,47), premultiplied before the Lanczos downscale so the white
  RGB under transparent pixels cannot halo, 113×96, 6 frames, 1.32 s, ~33 KB.
  Used for the two hit-counter icons and the paid-queue marker.

**Deferred (not fixed):** the rating row can still grey out while a song is
audible when the state poll's `current_seq` is ahead of the client's audio
clock (and a poll with no `rating` calls `reset()` for the rest of the song).
Recorded as a known issue in `RADIO.md` §9.1. Intended fix: keep the row open
until `pd:nowplaying` moves off the song and let a late click take the
server's 409.
