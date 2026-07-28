# Roadmap

What the engine doesn't do yet, and what it deliberately never will.

For what already runs and how, see [`README.md`](README.md); for the
self-verification/event-contract design, [`RUN_MODE.md`](RUN_MODE.md)
(its own "Not yet built" section covers stall-then-resume and live/
mid-connection worker-driven mutation — not repeated here).

## Open capability gaps

**Provider/API-level faults.** Every fault today attaches to a channel
stream; the playlist/EPG/Xtream endpoints themselves can't fail. A common
real-provider behavior — "the playlist call itself times out or 500s" —
isn't expressible yet. This is a missing `phase` axis (trigger × phase ×
action): only the stream phase exists today.

**Expired/rotating credentials, rotating stream URLs.** Auth currently has
one mode (present/absent, checked once at request time). Mid-session
expiry and URL rotation fall out naturally once auth grows a second mode.

**Configurable HTTP response metadata.** Small additive fields (custom
headers, etc.) beyond what scenarios can already set.

**Remaining HTTP faults.** Incorrect `Content-Length`, invalid chunked
encoding, a delayed response body distinct from a stall, and a midstream
reset distinct from `close_after_chunks`.

**Remaining MPEG-TS faults (worker era — needs live/request-time
mutation, not just pre-baked fixtures).** Packet drops and duplication,
PCR jump/drift, configurable PTS/DTS offset (today's
`timestamp-discontinuity` scenario is one pre-baked case, not a
parameter), PID replacement, PMT version change, and declared-vs-actual
bitrate mismatch.

**Live synthetic media generation.** Runtime resolution/framerate/GOP
knobs. Today's stance is pre-generated, hash-verified fixtures under
`fixtures/synthetic/` — see that folder's `README.md` for exactly what
exists and how it was made.

## Deliberately descoped (with rationale, not silently dropped)

- **Multiple provider profiles in one engine instance.** Run multiple
  instances/containers instead — already the proven pattern, and a
  Compose file makes it one stanza per provider. Revisit only on a
  concrete need that the multi-instance pattern can't satisfy.
- **Per-client fault targeting / the engine broadcasting one source to
  many clients.** The consuming proxy (not this simulator) is the
  component that fans a single upstream out to many clients. Modeling
  per-connection sources here is the correct shape for testing that.
  Revisit only if a real scenario needs the simulator itself to
  broadcast.

## Non-goals

This project exists to break in specific, repeatable ways — not to become
a second product. It will not grow into:

- Full Xtream Codes compatibility, a reseller panel, or production user
  management.
- A playlist editor, DVR, or general media-library manager.
- A general transcoding service or hosted public stream generator.
- A full media-server replacement, or guaranteed compatibility with every
  competing proxy.
- A public benchmark/leaderboard service, or a separate dashboard product.
