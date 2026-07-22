# Text encoding policy

Repository source, documentation, commit messages, and pull-request metadata
must use UTF-8. The repository contains Chinese product text, so a terminal
displaying mojibake is not evidence that a file should be re-encoded.

On Windows PowerShell 5.1, dot-source the bootstrap before passing non-ASCII
text to Git, GitHub CLI, Python, or another native process:

```powershell
. .\scripts\use-utf8.ps1
```

For commits and pull-request descriptions, prefer a UTF-8 file passed with
`git commit -F <file>` or `gh pr create --body-file <file>` rather than piping
Chinese text through a shell. When inspecting a UTF-8 file in Windows
PowerShell 5.1, use `Get-Content -Encoding UTF8`.

`scripts/check_text_encoding.py` runs in the lightweight GitHub Actions gate.
It checks raw Git-tracked bytes, PR title/body, and newly introduced commit
messages. It rejects invalid UTF-8, replacement characters, high-signal
GBK/Latin-1 mojibake, and lost-text `??` placeholders in PR metadata or commit
messages. The checker does not rewrite text automatically; a failed check must
be fixed at its source.
