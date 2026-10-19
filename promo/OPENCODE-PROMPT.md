Set up a complete Remotion motion-graphics studio on this Windows PC's Desktop, using the kit in a folder named `remotion-friend-kit`. When you're done, I should be able to open opencode in the new workspace, ask for a motion graphics video, and get a rendered MP4. Work through the steps in order. Every step can be run again safely, so if you get interrupted or I restart you, check what's already done and carry on from there.

Rules for the whole job:
- Don't delete or overwrite anything that already exists. If a target folder or file is already there, stop and ask me.
- Don't install anything except what's listed here. Use `winget` for programs and `npm`/`npx` for packages.
- Don't leave servers running (`npm run dev` and `remotion studio` never exit).
- Talk to me in plain language. Tell me what happened and what I'd see, not function names.

## 1. Find the paths

- Desktop: run `powershell -NoProfile -Command "[Environment]::GetFolderPath('Desktop')"`. It's often under OneDrive, not `C:\Users\<name>\Desktop`. Call the result DESKTOP below.
- Home folder: `%USERPROFILE%`. Call it HOME.
- The kit: look for a folder `remotion-friend-kit` containing `OPENCODE-PROMPT.md`, first in `HOME\Downloads`, then DESKTOP, then HOME. If there's only `remotion-friend-kit.zip`, extract it into its own folder next to the zip with `Expand-Archive`. If you can't find it, ask me where it is. Call its folder KIT.
- Note which shell your commands run in (PowerShell, cmd or Git Bash), and write commands for that shell.

## 2. Install what's missing

Check each one and install only what's missing:

| Needed for | Check | Install |
| --- | --- | --- |
| Remotion (Node 20 or newer) | `node -v` | `winget install -e --id OpenJS.NodeJS.LTS` |
| Downloading skills from GitHub | `git --version` | `winget install -e --id Git.Git` |
| Video review and audio mixing tools | `ffmpeg -version` and `ffprobe -version` | `winget install -e --id Gyan.FFmpeg` |
| Beat detection (optional) | `python --version` (3.10 or newer), then `python -c "import librosa"` | `winget install -e --id Python.Python.3.12`, then `python -m pip install librosa` |

Add `--accept-package-agreements --accept-source-agreements` to winget so it doesn't wait for input.

**PATH warning:** opencode's environment was fixed when it started, so a program you just installed may still say "not recognized". Look in the usual install folders (`C:\Program Files\nodejs`, `C:\Program Files\Git\cmd`, the winget packages folder under `%LOCALAPPDATA%\Microsoft\WinGet\Packages\` for ffmpeg) and either run it by full path or put that folder at the front of PATH for that command. If that doesn't work, tell me to restart opencode and paste this prompt again.

If Python or librosa fails to install, skip it and tell me. Only beat detection needs it.

## 3. Copy the workspace to the Desktop

1. If `DESKTOP\remotion` or `DESKTOP\motion-kit` already exists, stop and ask me.
2. Copy `KIT\workspace` to `DESKTOP\remotion` and `KIT\motion-kit` to `DESKTOP\motion-kit`, everything included. With robocopy, exit code 1 means success; 8 or higher means failure.
3. Several files say `{{DESKTOP}}`. Replace every `{{DESKTOP}}` with the real DESKTOP path in `DESKTOP\remotion\AGENTS.md` and `DESKTOP\remotion\_motion-theory\README.md`. Use your edit tool, one change at a time.
4. The motion-kit files were written on another PC and point at `C:\Users\PC\Desktop`. In `DESKTOP\motion-kit\README.md` and every `.md` in `DESKTOP\motion-kit\prompts\`, replace `C:\Users\PC\Desktop` with the real DESKTOP path, and replace "Claude Code" with "opencode". Two more changes:
   - `prompts\README.md`: the "Which workspace" section lists three workspaces. Only `remotion` exists on this PC. Rewrite the section to say every prompt is used in `DESKTOP\remotion`, and delete the code-motion and hyperframes lines.
   - `prompts\05-director-brief.md`: it mentions an article at `C:\Users\PC\Downloads\movez-article\article.md`. That file isn't included. Delete the sentence that points to it.
5. Afterwards, search both folders for `{{DESKTOP}}` and `Users\PC` and confirm there are none left.

## 4. Install the skills

Opencode loads skills from `HOME\.config\opencode\skills\<name>\SKILL.md` (also from `HOME\.claude\skills` and `HOME\.agents\skills`).

1. First check whether any `remotion-*` skills already exist in any of those three folders. If they do, tell me where and ask before continuing. Two copies with the same name conflict.
2. From HOME (not from inside a project), install Remotion's official skills, the latest version from GitHub:
   `npx -y skills add remotion-dev/skills --global --agent opencode --skill '*' --yes`
   In Git Bash, keep the quotes around `*`. In cmd, use `"*"`.
3. Check that `remotion-best-practices`, `remotion-markup`, `remotion-render`, `remotion-create`, `remotion-captions`, `remotion-studio`, `remotion-docs`, `remotion-upgrade`, `remotion-interactivity`, `remotion-maps`, `remotion-saas` and `remotion-multimedia` each have a `SKILL.md`, and tell me which folder they ended up in. It should be one of the three skill folders above. If the installer failed or put them somewhere else, copy the matching folders from `KIT\skills\` into `HOME\.config\opencode\skills\` instead. That copy is a tested snapshot from September 2026.
4. Copy `KIT\skills\frontend-design` into `HOME\.config\opencode\skills\` if no `frontend-design` skill exists yet. It guides layout, typography and color choices.
5. Check that no `.agents` folder or `skills-lock.json` was created inside `DESKTOP\remotion` or any folder in it. If one was, tell me before deleting it.

## 5. Point opencode at the workspace from anywhere

Opencode reads `AGENTS.md` in the folder it's started in and the folders above it, plus one global file, `HOME\.config\opencode\AGENTS.md`.

- **If the global file exists**, add the section below at the end. Don't change anything else in it.
- **If it doesn't exist but `HOME\.claude\CLAUDE.md` does**, stop and ask me. Opencode only reads that Claude file while its own global file is missing, so creating one would quietly switch those instructions off.
- **If neither exists**, create the global file with the section below.

```markdown
## Videos and motion graphics

Motion graphics and videos on this PC are made with Remotion in the workspace at
`DESKTOP\remotion` (write the real path here). For any video request:
read `DESKTOP\remotion\AGENTS.md` first and follow it. Each video gets its own
folder inside that workspace, copied from `starter\`. Never build a video anywhere else.
```

## 6. Prove it works end to end

Run each check and look at the result yourself:

1. In `DESKTOP\remotion\starter`: `npm ci --prefer-offline --no-audit --no-fund`, then `npm ls --depth=0` (nothing should be listed as missing), then `npm run lint` (should pass). Newer npm may warn that esbuild's install script was blocked ("allow-scripts"). Ignore it; rendering works without it.
2. Render one frame: `npx remotion still SetupTest out/check.png --frame=150 --scale=0.5`. Open the PNG. If you can't view images, tell me the path and ask me to look. It should show a dark frame with a blue ring around a number and the words "skills loaded". The first render downloads Remotion's own browser (about 100 MB, once). That's expected. Don't point Remotion at Microsoft Edge instead: current Edge quits on launch and every render fails.
3. Render the full video: `npx remotion render SetupTest out/setup-test.mp4 --concurrency=1 --crf=14`. It should run about 8 seconds, 1920×1080.
4. Test the review tool: `node "DESKTOP\motion-kit\tools\review.mjs" "DESKTOP\remotion\starter\out\setup-test.mp4" --at 4`. Confirm it wrote `contact.png`, `strip-4.png`, `phone.png` and `loop_check.mp4`, and open `contact.png`.
5. Test the sound tools: `node "DESKTOP\motion-kit\tools\sfx.mjs" "DESKTOP\motion-kit\_selftest-example\cues.json" "DESKTOP\remotion\starter\out\sfx.wav" --dur 8`, then `node "DESKTOP\motion-kit\tools\mix.mjs" --video "DESKTOP\remotion\starter\out\setup-test.mp4" --audio "DESKTOP\remotion\starter\out\sfx.wav:1.0" --out "DESKTOP\remotion\starter\out\with-sound.mp4"`. It should print the loudness before and after, ending near −14 LUFS.
6. If librosa installed: `python "DESKTOP\motion-kit\tools\beats.py" "DESKTOP\remotion\starter\out\with-sound.mp4"` should print JSON with a `bpm` value. The number doesn't matter; it only shows the tool runs.
7. Don't install the projects in `examples\`. They're reference code, installed only when someone wants to render them.

If a check fails, fix the cause and run it again. Don't change the starter's animation code to make a check pass, because that code is known to work. A "cannot find module" error means the install is broken, not the code.

## 7. Report back

Tell me, in plain language:
- What was installed, and what was already there.
- Where everything lives, with full paths: the workspace, motion-kit, the skills folder, the global AGENTS.md, and the test video `DESKTOP\remotion\starter\out\with-sound.mp4` so I can watch it.
- Anything that failed or was skipped, and what that means I can't do yet.
- How to make a video from now on: open opencode in `DESKTOP\remotion` and ask, for example, "make a 10-second 1080×1920 motion graphic that introduces my channel, bold kinetic type, orange accent". For bigger jobs, use a template from `DESKTOP\motion-kit\prompts\` (its README says which one to pick). After every render, ask for the critique pass (`06-critique-pass.md`).
