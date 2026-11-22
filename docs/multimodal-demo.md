# Model choice, dictation and project illustration

These are implemented product paths using the configured OpenAI API account.
They complement the existing engineering conversation and separate photo-inspection
workflow. They do not add hardware control or replace the source CAD.

## Choose the conversation model

Open **Model & settings** above chat to choose GPT-6.1 Terra or GPT-6.1 Sol.
The backend checks which models the account advertises. The choice applies to
new chat, layout, estimate and photo-analysis requests. Each request records the
model reported by the provider when available. Prior results keep their original
attribution. Opening a portable project starts a session with the configured default.

## Dictate, review, then send

1. Select **Dictate**, allow microphone access, and speak for up to 60 seconds.
2. Select **Stop and transcribe**. The recording is sent to OpenAI for transcription with `gpt-transcribe`.
3. Review the editable chat draft, especially numbers and units. Correct any
   transcription error, then select **Send** yourself.

Dictation never sends a chat message automatically. Cancel, denied permission,
silence and provider failures have explicit states. An overlong transcript remains
available to copy instead of silently truncating or replacing the existing draft.
Recording stops and microphone tracks are released on cancellation or navigation.

The browser records mono audio in memory and sends PCM16 WAV at 16 kHz. The server
also accepts 24 kHz, validates the complete WAV, strips metadata and caps duration,
size, concurrency and response time. Credentials stay server-side. The app does
not save raw recordings or log their text; separately collected synthetic test
evidence is identified in the verification record. Microphone capture requires
localhost or a browser-supported secure context.

English number words and spelled scientific units have a bounded deterministic
normalization path. It supports examples such as **five milligrams per milliliter**
and **two milliliters per tube**. Ambiguous totals, ranges, rates and missing units
still require clarification. This is a limited supported grammar, not universal
speech or scientific-language understanding.

## Illustrate an assembly step

Open **Assembly**, choose a step by title and select **Illustrate this step**.
GPT Image receives the actions for that step and a reference derived from the
current project revision. For a plate holder, the reference shows the holder,
the plate and the well layout. For a procedural check without geometry, the model
receives the written sequence.

Select the resulting image to enlarge it, review its sources or download the PNG.
You can return to a recently illustrated step in the same project and revision
without generating it again. Save the PNG if you want to keep it after signing out.
Follow the written guide and source drawings when assembling the hardware.

## Create an optional presentation image

With an accepted design, open **Project details → Project illustration** and
select **Generate with GPT Image**. The app captures the actual current 3D view,
sends that reference once and displays the resulting image beside a comparison
with the original view. The result is downloadable as PNG and clearly labelled
**AI-generated illustration — not a technical drawing**.

Generation has a real waiting state, cancellation and a revision guard. It makes
no automatic retry, project edit or CAD export. Images are not inserted into the
portable project, engineering package or assembly instructions. Reopening or
changing the project clears a stale illustration. The backend accepts bounded
PNG/JPEG data only, strips metadata and sends the reference and prompt to the OpenAI image edits endpoint.

The historical Meta example below preserved the recognizable overall composition but changed
small mechanical details. They are useful presentation illustrations; source CAD,
the deterministic model and engineering instructions remain authoritative.

| Actual project reference | Actual Muse Image result |
| --- | --- |
| ![Original rendered project](images/multimodal-reference.png) | ![AI-generated presentation illustration, not a technical drawing](images/multimodal-illustration.png) |

This pair is from one recorded real request on confirmed revision 9. The original
Jubilee design is by Joshua Vasquez / Machine Agency, CC BY 4.0; see
[source attribution](../THIRD_PARTY_NOTICES.md). Image generation changes details.

## Usability changes

- A failed **New project** request retains the current work. A successful reset
  offers **Undo new project** for the previous in-memory workspace.
- Visible shortcuts reveal and focus the preparation plan or project files.
- The opening composer is reachable in the first mobile viewport; populated
  project controls wrap within the screen.
- Native keyboard controls rotate and zoom the 3D view. Source loading and
  failure are reported separately from the rendered scene.
- The hidden import input no longer creates an invisible extra keyboard stop.

These improvements were evaluated through agent walkthroughs and controlled
browser tests. They are not external-user interviews or a measured usability study.
See [delivery verification](verification.md) for actual live failures, corrections,
request counts and the distinction between live inference and offline fixtures.

## Provider references

OpenAI provides the API contracts for [recorded dictation](https://developers.openai.com/api/docs/guides/speech-to-text) and [image edits](https://developers.openai.com/api/reference/resources/images/methods/edit).
The screenshot pair above records the previous provider's behavior.
