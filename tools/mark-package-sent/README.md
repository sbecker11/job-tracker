# Mark package sent (`mps://`)

Installs a tiny macOS helper so Contact priority **Mark sent** can write
`awaiting_response_since` (Wait stage) without opening a terminal.

```bash
cd job-tracker
.venv/bin/pip install -e .
./tools/mark-package-sent/install.sh
```

Then `mps://mark?key=<normalized_key>` shells out to `mark-package-sent`.
