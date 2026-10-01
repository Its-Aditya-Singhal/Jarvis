# User guide

JARVIS is a voice assistant for your Mac that only works for you. It listens
for its name, checks every request against your voiceprint, and does
everyday Mac things, your Gmail, Google Drive and Google Calendar, and
anything else you ask in your own words.

## What you need

- A Mac with Apple Silicon (M1 or newer) and macOS 14 Sonoma or later
- About 2 GB of free disk space (speech, voice and voiceprint models)
- A microphone (the built-in one is fine). No camera is needed.
- An internet connection: for the first-run download, and for the AI (Google's
  Gemini API, free tier) and your Google account

## Install

1. Download `JARVIS-1.0.0-arm64.dmg` from the
   [latest release](https://github.com/Its-Aditya-Singhal/Jarvis/releases/latest).
   Optionally compare its SHA-256 with `SHA256SUMS.txt` on the same page:
   `shasum -a 256 ~/Downloads/JARVIS-1.0.0-arm64.dmg`.
2. Open the .dmg and drag **JARVIS** onto **Applications**.
3. Open JARVIS from Applications. The app is signed but not notarized by Apple,
   so macOS stops it the first time. Go to **System Settings → Privacy &
   Security**, scroll down, and press **Open Anyway** next to JARVIS. You only
   do this once.
4. macOS asks for the **microphone**. Allow it: JARVIS can't hear or recognise
   you without it.

## First run

**Models.** The first screen lists the on-device models (speech recognition,
the voice, the voiceprint model) and their size. Press **Download**. Each file
is checked against its SHA-256 before it is used, and an interrupted download
resumes where it stopped.

**Setup.**

1. Your name, and the assistant's name. Its name is also the wake word, so
   pick something you'll say easily (JARVIS, FRIDAY, EDITH…).
2. A female or male voice.
3. **Voice enrollment** (about five minutes, in a quiet room): read 24 short
   phrases in English, Hindi and Hinglish. Some ask you to speak softly, a
   little louder, or from a step back from the Mac, so the voiceprint knows how
   you really talk. Each recording is checked (the right words, loud and clear
   enough, sounds like your earlier ones) before it counts. Only an encrypted
   voiceprint is kept, never the recordings.

## The AI: a free Gemini API key

JARVIS's brain is Google's Gemini API, on the free tier. Nothing heavy runs on
your Mac.

1. Open [Google AI Studio → API keys](https://aistudio.google.com/apikey)
   (**Settings → AI → Get a free key** opens it) and sign in with your Google account.
2. Press **Create API key** and copy it.
3. Paste it into **Settings → AI → Gemini API key** and press **Save key**, then
   **Test**. Never paste the key into a chat or a file: JARVIS encrypts it with
   your Mac's Keychain key and only ever shows its last four characters.

Two models, both editable in Settings → AI:

- **Commands model** (default `gemma-4-26b-a4b-it`, about 30 requests a minute
  free): understands requests that aren't everyday commands.
- **Writing model** (default `gemini-3.5-flash-lite`, about 15 a minute): mail
  summaries, drafts, document summaries.

When one model's free limit runs out, the other answers and JARVIS says so.
When both are used up for the day it tells you; the daily limit resets at
midnight Pacific time. Everyday commands (volume, brightness, timers, music,
screenshots, apps, the time) never use the AI and always work.

Google may use free-tier requests to improve its products; see
[SECURITY.md](SECURITY.md#the-ai-and-your-google-account) for what is sent.

## Gmail, Google Drive and Google Calendar

Google doesn't give API keys for these, so you create your own free OAuth
client once (about five minutes). **Settings → Google account → How to set up**
shows the same steps:

1. Open [Google Cloud Console](https://console.cloud.google.com/apis/credentials)
   and create a project (any name, e.g. "JARVIS").
2. **APIs & Services → Library:** enable **Gmail API**, **Google Drive API** and
   **Google Calendar API**.
3. **OAuth consent screen:** User type **External**, fill in the app name and
   your address, add your own address as a **test user**. Then **Publish app →
   In production** (otherwise Google signs you out every 7 days). You'll see
   "Google hasn't verified this app" when you connect: it's your own app, so
   choose **Advanced → Continue**.
4. **Credentials → Create credentials → OAuth client ID**, application type
   **Desktop app**.
5. In JARVIS, paste the **client ID** and **client secret** (or the downloaded
   JSON file) into Settings → Google account and press **Save client**.
6. Tick the services you want (Gmail, Google Drive, Google Calendar) and press
   **Connect**. Your browser opens Google's consent page: pick your account,
   tick every box, and allow. The page then says "Connected" and Settings shows
   your address.

Only the scopes of the services you ticked are requested: reading mail, saving
drafts and sending (Gmail), reading files (Drive, read-only), and reading and
adding events (Calendar). **Disconnect** revokes the sign-in at Google and
deletes it from the Mac.

## Talking to it

Say its name, then what you want, in English, हिंदी or Hinglish. Say just the
name and it answers "Yes boss, how may I help you?" straight away.

**Everyday:**
- "JARVIS, set a timer for five minutes." / "kal subah saat baje jagana."
- "volume 30" / "next song" / "dim the screen" / "turn on dark mode" / "take a screenshot"
- "open Safari" / "WhatsApp band karo" / "open my downloads folder"
- "start the stopwatch" / "search for python tutorials" / "play believer on YouTube"
- "what's 18% of 2400?" / "how many days till Diwali?"

**Mail, files and calendar:**
- "summarize my last 10 emails" / "any new mail?"
- "what did Rahul mail me?" → "okay, send him a mail confirming I'll come"
- "draft a mail to priya@example.com asking for the slides" → "send it"
- "find the budget sheet in my drive" → "summarise it" / "open it"
- "what's on my calendar tomorrow?" / "add dinner with Rahul on Friday at 8"

**Messages, calls, reminders:**
- "text Mom that I'll be home by 8" / "WhatsApp Rahul that I'm on my way" (read back first, see below)
- "read my messages" / "what did Mom text me?" → "reply to her saying yes"
- "call Dad" / "FaceTime Priya" (asks first)
- "remind me to call the bank tomorrow at 10" / "put eggs on my shopping list" / "what are my reminders for today?"
- "add a note in Apple Notes saying the gate code is 4512" / "remember that my car is on level 2"

**Weather and the web:**
- "my city is Pune" (once) → "how's the weather?" / "will it rain tomorrow?" / "weather in Mumbai"
- "what's the news today?" / "who won the match last night?" / "100 dollars in rupees" / "bitcoin price"

**Files, calendar, focus, music, clipboard:**
- "summarise the PDF I downloaded yesterday" / "read the lease and tell me the notice period"
- "move it to Documents" / "rename my latest download to tax return 2026" (asks first)
- "what's my day look like?" / "when am I free tomorrow?" / "set up a meeting with Rahul tomorrow at 3" (asks first, then Google emails the invitation)
- "start focus mode" / "focus for 45 minutes" / "end focus mode" / "turn on Do Not Disturb"
- "play Believer" / "play my workout playlist" / "what's playing?" / "set Spotify's volume to 30"
- "summarise what I copied" / "translate my clipboard into Hindi" / "fix the grammar of what I copied"

**Your day and your screen:**
- "give me my daily briefing": weather, the rest of today's calendar, reminders due and unread mail
- "Jarvis, what's happening on my screen?" / "what does this error mean?": JARVIS takes one
  screenshot, sends it to Gemini's writing model (Gemini 3.5 Flash-Lite unless you changed it in
  Settings → AI), and says what's going on, what the error means and what it can do for you. The
  screenshot leaves the Mac only for that request, is never saved, and needs the Gemini brain (the
  local model can't see images)

**Anything else:** ask in your own words. The AI picks from all of JARVIS's
tools; for Mac apps no tool covers ("close all my Safari tabs", "turn off
Wi-Fi") it writes an AppleScript, runs it straight away if it only reads, and
shows it to you before it changes anything.

### Sending messages and calling

Like mail, a message is **read back first** ("Here's the iMessage to Mom (+91 98765 43210): …
Shall I send it?") and goes out only after "yes, send it" in your verified voice, or a click on
**Send**. iMessage goes through the Messages app (SMS if your iPhone relays texts). WhatsApp has
no way for other apps to send, so JARVIS opens the chat with your message written in and presses
Return only when WhatsApp is in front; otherwise it leaves the message there for you to send.
WhatsApp needs the number with its country code: numbers saved without one get your Mac's region
code (+91 in India), and the read-back says the full number. Calls go through FaceTime (and your
iPhone for phone calls); macOS may ask you to press Call.

### Permissions the everyday tools ask for

macOS asks once for each, the first time it's needed:

| Tool | Permission |
|---|---|
| Reading messages, finding contacts | **Full Disk Access** for JARVIS (System Settings → Privacy & Security → Full Disk Access); without it contacts fall back to the Contacts permission and reading messages says how to allow it |
| Sending iMessages, Reminders, Music | **Automation** (“JARVIS wants to control Messages / Reminders / Music”): click OK |
| Pressing Return in WhatsApp | **Accessibility** (the same one typing uses) |
| Screen help | **Screen Recording** (the same one screenshots use) |

**Do Not Disturb:** macOS doesn't let apps switch Focus directly, so JARVIS runs two shortcuts
you make once in the Shortcuts app: a shortcut named **JARVIS Focus On** with the action
*Set Focus → Do Not Disturb → Turn On*, and **JARVIS Focus Off** with *Turn Off*. Without them,
focus mode still quits distracting apps and runs the timer, and tells you this.

### Sending mail

A mail is never sent directly. JARVIS writes it, saves it as a Gmail draft,
**reads it back aloud** and shows it in a confirmation card. It goes out only
after you say "yes, send it" in your verified voice, or click **Send**. Say
"no" (or let the card expire) and it stays a draft in Gmail.

### The conversation window

After each reply JARVIS keeps listening without its name for a minute
(Settings → Voice: 30 s, 1 min or 2 min), and remembers what was just said, so
"him", "that mail" and "send it" work. Each follow-up is checked against your
voice too. Say "thanks" or "that's all" to end it; if someone else speaks, the
window closes on its own.

## Why it sometimes says no

Every request is checked against your voiceprint on its own words:

| Your voice on that request | What runs |
|---|---|
| Recognised | everything (deleting, sending, calling, inviting, moving or renaming files still asks for your "yes") |
| Unclear (a short or noisy clip) | only harmless everyday commands: volume, brightness, timers, music, screenshots, searches, the weather, the time |
| Someone else's | nothing ("That voice doesn't match my owner") |

If it says "I couldn't confirm your voice", say it again a little longer. A
very short follow-up ("louder") counts as unclear, which is fine for the
everyday commands; for anything that reads your mail or files, say a full
sentence. Typed commands are off by default, because the keyboard would bypass
the voice check; you can allow them in Settings → Security → Commands.

## Keyboard

| Keys | Does |
|---|---|
| ⌘K | command palette: jump to any view or action |
| ⌘1 … ⌘7 | System, Authentication, Memory, Tools, Security, Privacy, Settings |
| / | focus the command bar |
| Esc | close the palette or the About view |
| ⌘I | About JARVIS |
| ⌘, | Settings |

## Privacy

- **Privacy view:** every kind of stored data with its size and age. Export a
  readable copy, delete any part, or factory-reset. Each needs your confirmation.
- **Offline mode** (on by default): the engine can't reach the internet except
  the services you switched on (the Gemini API once a key is saved, Google's
  APIs once your account is connected). Everything it tries is listed.
- **Memory view:** facts it remembers and your conversation history (kept 30
  days by default; off, 7 days or forever).
- Audio is never stored. Speech that doesn't start with its name (outside a
  conversation window) is thrown away straight after transcription.

More in [SECURITY.md](SECURITY.md).

## Settings

Identity (names, re-record your voice), the AI (Gemini key, the two model
names, or a local model through Ollama), Google account, voice (speed, the
greeting, the conversation window), microphone, security presets (voice match
Strict by default, typed commands, generated AppleScript), performance mode,
on-device models, memory, file-search folders and optional Apple Calendar /
Notes sync.

## Troubleshooting

| Problem | Fix |
|---|---|
| "JARVIS can't be opened" | System Settings → Privacy & Security → Open Anyway (see Install) |
| "jarvis-backend wants to access key jarvis-assistant in your keychain" | Type your Mac login password and press **Always Allow**. JARVIS keeps its encryption key in the Keychain. Builds are signed with a local identity made once on this Mac, so macOS asks once and remembers it across updates. If you press Deny, your saved voice is not lost: quit, reopen and press Always Allow |
| "AI not reachable" / "no Gemini API key" | Settings → AI: save a key and press Test. Check the internet connection |
| "My free Gemini limit is used up" | Wait a minute (per-minute limit) or until midnight Pacific (daily limit); everyday commands keep working. You can switch the model names in Settings → AI |
| "Gmail isn't connected" / "reconnect in Settings" | Settings → Google account → Connect, and tick every box on Google's consent page |
| Google signs you out after a week | The OAuth consent screen is still in Testing: publish it (In production), then reconnect |
| "I couldn't confirm your voice" a lot | Speak a little longer and closer; if it keeps happening, re-record your voice in Settings → Identity in your usual room. If it refuses because it can't confirm your voice, press **Use Mac password** |
| Settings says "needs level 2" | Say "Jarvis, hello" and try again within a minute, or press **Use Mac password** at the top of Settings (two minutes) |
| Voice commands are off | Settings → Microphone says why and how to fix it (permission, no device, the wrong device, busy) |
| A model is missing | Settings → On-device models → Download, then Restart |
| Screenshot / dark mode / typing don't work | macOS asks once for Screen Recording, Automation (System Events) and Accessibility; allow JARVIS in System Settings → Privacy & Security. If the switch is already on but it still asks, the switch belongs to an older build: remove JARVIS from that list with **–**, or run `tccutil reset ScreenCapture com.adityasinghal.jarvis`, then reopen JARVIS and allow it again |
| File search finds nothing | Allow JARVIS for Desktop, Documents and Downloads in System Settings → Privacy & Security → Files and Folders |
| The app shows "could not start" | Reinstall from the .dmg; if it persists, open an issue with the log from `~/Library/Application Support/JarvisAssistant/logs` |

## Uninstall

Use **Privacy → Factory reset** (it revokes the Google sign-in at Google and
removes the Keychain key), quit JARVIS, drag it from Applications to the Trash,
and delete `~/Library/Application Support/JarvisAssistant`. You can also delete
the OAuth client in Google Cloud Console and the API key in AI Studio.
