# The Firefox add-on

The add-on brings Tsuyaku to the normal youtube.com in Firefox:

* **English subtitles on YouTube's own player**, also in fullscreen. Click a subtitle to see the
  Japanese. The **EN** button next to YouTube's CC button turns them on and off.
* **YouTube's chat, translated in place.** Click a message to see the Japanese; the **EN/JP**
  switch in the chat header flips them all.
* **The message box:** type English in YouTube's chat box. About a second after you stop, three
  Japanese versions appear above it, each translated back into English. **Tab/Enter** puts the
  selected one in the box, **Enter** again sends it.
* **A Tsuyaku button in the toolbar** starts and stops the models (speech recognition and the
  translator). Nothing runs until you turn it on.

Speech recognition and translation run on your PC; nothing is sent anywhere unless you choose a
cloud model for the message box.

## Install

1. **Install the Tsuyaku program** as described in the [README](../README.md#install)
   (`install.bat`). The installer also connects Firefox to it; `uv run tsuyaku firefox` in the
   Tsuyaku folder does that again if needed.
2. **Add the add-on to Firefox (version 142 or newer):** download `tsuyaku-firefox-<version>.xpi`
   from the [latest release](https://github.com/Chunibyodev/tsuyaku/releases/latest), drag it onto
   a Firefox window and click **Add**. Releases are signed by Mozilla, so release Firefox installs
   them like any add-on.
3. **Pin the button:** Firefox's puzzle-piece menu → Tsuyaku → *Pin to Toolbar*.
4. **Click it → Turn on.** On a fresh install, **Download** fetches the models first (about
   4–5 GB). Then open a YouTube video or live stream.

### Running the add-on from source

* **Try it:** open `about:debugging#/runtime/this-firefox`, click **Load Temporary Add-on…** and pick
  `src/tsuyaku/extension/manifest.json`. Firefox removes temporary add-ons when it closes.
* **Firefox Developer Edition, Nightly or ESR:** set `xpinstall.signatures.required` to `false` in
  `about:config`, then open the unsigned `.xpi` that `tsuyaku firefox` builds.
* **Sign your own build** (free, "unlisted" = not published on the add-on site): create API keys at
  <https://addons.mozilla.org/developers/addon/api/key/>, then
  `npx web-ext sign --channel unlisted --source-dir src/tsuyaku/extension --api-key <JWT issuer> --api-secret <JWT secret>`
  and open the `.xpi` from `web-ext-artifacts`. A fork must change the add-on id
  (`browser_specific_settings.gecko.id` in `manifest.json` and `EXTENSION_ID` in
  `src/tsuyaku/host/install.py`); each signed version needs a higher `version`.

## Using it

The badge on the toolbar button shows the state: **…** starting, **ON** ready, **↓** models to
download, **!** something failed (hover for the reason, or open the popup).

The popup has the switches: subtitles, a Japanese line above each subtitle, subtitle size, chat
translation, the message box and its tone, and "Turn on when Firefox starts". Its **This tab**
part shows what Tsuyaku is doing in the tab you're on: subtitle lines with the speech recognition
and translation times (and whether speech runs on cuda or cpu), and chat messages translated,
waiting and too old, with how long the last batch took.

**Turn off** stops the models and frees the GPU memory. Closing Firefox does the same.

**Settings…** (in the popup) opens the settings page: speech recognition (engine, Whisper model,
device, Whisper window, phrase cutting), the translation model (presets grouped by GPU size, GPU
layers, or your own OpenAI-compatible server), the message box's model (local, or a free Gemini /
Groq / OpenRouter key, with *Load list* and *Test*), and the glossary (for every stream, or per
channel you've watched). Saving reloads only what changed; a new model is downloaded from the
toolbar button's popup.

Under the model list the page says what the chosen model is good at, how much GPU memory it needs
next to the speech model, and whether your GPU has that much (it reads your NVIDIA card's name and
memory). A model that doesn't fit still runs: llama.cpp keeps the part that doesn't fit in system
memory, which is slower. The popup's Translator line names the model that is running. See the
README for which model to pick.

### Good to know

* **Subtitles come just after the speech, not with it.** YouTube's player can't be held back, so
  a line appears once it has been recognised and translated: about 1–1.5 s after the phrase ends
  on a GTX 1080 Ti-class GPU. Each line then stays on screen long enough to read.
* **It listens to what YouTube plays.** Subtitles work for anything the player shows: live
  streams, archives, members-only streams, any playback speed, seeking. No yt-dlp is involved.
  Ads are not transcribed.
* **Don't mute the video.** Firefox gives the extension the sound after YouTube's volume, so a
  muted video is silent to Tsuyaku. Turning the volume down is fine: Tsuyaku turns quiet sound
  back up (down to about 10% volume).

## Troubleshooting

* **"Tsuyaku's program isn't set up on this PC"**: run `install.bat` (or `uv run tsuyaku firefox`)
  in the Tsuyaku folder. `tsuyaku doctor` then shows `Firefox extension: connected`.
* **"Firefox couldn't start Tsuyaku's program"**: Firefox still points at a Tsuyaku folder that was
  moved or deleted. Run `install.bat` in the folder where Tsuyaku is now; it rebuilds the folder's
  Python environment and points Firefox at it.
* **A video went silent after the extension was reloaded or updated**: reload the YouTube tab.
  The sound runs through the extension's audio tap, which belonged to the old version.
* **"Click the video once so Tsuyaku can hear it"**: Firefox lets a page use sound once you have
  interacted with it (or allowed autoplay for YouTube).
* **"Another extension is already using this video's sound"**: an equalizer or volume-booster
  extension has the same audio tap; turn it off for YouTube.
* **Chat stays in Japanese**: look at the popup's *This tab → Chat*. "Waiting for the translator
  to start" goes away once the Translator line is ready. Many messages waiting with long batches
  means the GPU is overloaded (another program using it, or a model too big for it: try a Q4 model
  on the settings page). A busy chat can outrun a local model; the newest messages go first, and
  ones that waited too long get a 文A button.
* **Translator or speech model failed**: see `tsuyaku.log` and `llama-server.log` in Tsuyaku's
  logs folder (`tsuyaku doctor` prints where); `tsuyaku.log` also has a line per chat batch.
  Firefox's Browser Console (Ctrl+Shift+J) shows the host's warnings too.
* **Disconnect Firefox** again: `uv run tsuyaku firefox --remove`.

## How it works

```
YouTube tab   watch.js: taps the <video>'s sound ── PCM + video time ──┐
              draws the subtitles in the player ◄── cues (video time) ──┤
YouTube chat  chat.js ◄──────────── translations, message box ──────────┤
                                                                         │
              background.js ◄── native messaging (stdin/stdout) ──► tsuyaku-host
                (the toolbar button = this connection)                  VAD → Whisper → llama-server
```

* **Turning on** opens a native messaging connection (`browser.runtime.connectNative`). Firefox
  starts `tsuyaku-host` from Tsuyaku's Python environment; `tsuyaku firefox` registered it
  (Windows: registry key `HKCU\Software\Mozilla\NativeMessagingHosts\tsuyaku` pointing at a
  manifest in Tsuyaku's data folder; Linux: `~/.mozilla/native-messaging-hosts/tsuyaku.json`).
  The host loads the speech model and starts llama-server, and reports progress to the popup.
  **Turning off** closes the connection: the host stops llama-server and exits. On Windows
  Firefox also ends the whole process tree.
* **Subtitles** (`content/watch.js`, `host/service.py`): the video's sound is routed through a
  Web Audio graph (it keeps playing as before) and copied in ~85 ms buffers, tagged with the
  video's time, speed and volume. Only after the audio context is running is the video routed
  through it, so the tap can never mute a video. The host undoes the volume, resamples to
  16 kHz and puts the audio on a continuous clock of its own: a seek, an ad or "jump to live"
  leaves a gap, which closes the phrase being spoken. Then the pipeline runs (VAD → speech
  recognition → subtitle translation), and each line is mapped back to the video's timeline,
  even at 1.25× or 2×. The page draws the lines inside `#movie_player`, so they follow the
  player into fullscreen.
* **Chat and message box** (`content/chat-bridge.js`, `content/chat.js`): `chat.js` translates
  YouTube's chat in place and runs the message box; `chat-bridge.js` connects it to the
  extension's messaging. Nothing is sent before the translator is ready. The stream's title and
  channel (from YouTube's player) give the translator context and pick the channel's glossary.
* **Settings** (`options/`, `host/settings.py`): the page reads and saves Tsuyaku's config.toml and
  glossary through the host, which reloads the speech model or restarts the translator only when
  their settings changed.
* Each page connection gets an id; the host keeps one subtitle pipeline per YouTube tab and one
  chat translator per chat frame, and drops them when the page goes away.
