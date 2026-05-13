# Practice quiz tools

Two Python scripts work together: one scrapes the AA Systems Practice Quiz into SQLite (and optional images), the other builds a study PDF from the saved questions.

**Personal study only.** Comply with your employer or training provider’s terms of use. The scraper targets a specific training site; if login is required, use browser cookies as described below.

## Setup

Python 3.10+ recommended.

```bash
pip install -r requirements.txt
```

Dependencies: `requests`, `beautifulsoup4`, `lxml`, `reportlab`.

---

## `quiz_scraper.py`

Automates the ASP.NET practice quiz flow (fleet selection, module options, submit/next), parses graded pages, and writes every scraped question into **`quiz_items`** for that run. **`question_bank`** keeps one row per `question_text`: new questions are inserted; existing ones are **updated** (options, correct answer, `image_relpath` when a new graphic is present, `last_seen_at`, `seen_count`).

### Quick start

```bash
python quiz_scraper.py
```

Defaults: SQLite **`quiz_bank.sqlite`**, fleet **A32F**, **100** questions, all modules, **1** second delay between POSTs, question images under **`quiz_media/`**.

### Useful options

| Option | Description |
|--------|-------------|
| `--db PATH` | SQLite file (default `quiz_bank.sqlite`) |
| `--runs N` | Number of full scrape passes from the quiz start |
| `--fleet {A32F,B737,B777,B787}` | Fleet |
| `--limit N` | Requested question count (`txtQuesLimit`) |
| `--module-mode {all,major,certain}` | All systems, major systems, or selected modules only |
| `--each-module` | With `certain`: one quiz per module checkbox on the params page |
| `--modules mod000,mod210` | With `certain`: comma-separated module ids only |
| `--cookies PATH` | Netscape `cookies.txt` from a logged-in browser |
| `--delay SECONDS` | Pause between quiz POSTs (default `1`) |
| `--pick VALUE` | Radio value submitted each time (default `radDist1`; correct answer is read from the graded page) |
| `--media-dir DIR` | Folder for downloaded question graphics (default `quiz_media`) |
| `--no-graphics` | Do not download `getGraphic.ashx` images |

`--media-dir` is resolved from the **current working directory**. Stored paths look like `quiz_media/<filename>.png` and are written to **`image_relpath`** on `question_bank` and `quiz_items`.

### Examples

```bash
# B737, slower pacing, custom DB
python quiz_scraper.py --fleet B737 --delay 2 --db my_bank.sqlite

# Only two modules, with auth cookies
python quiz_scraper.py --module-mode certain --modules mod000,mod210 --cookies cookies.txt

# Save all modules
python quiz_scraper.py --module-mode certain --each-module --fleet A32F --limit 100

# Scrape without saving images
python quiz_scraper.py --no-graphics
```

---

## `export_quiz_pdf.py`

Reads **`question_bank`** from the same SQLite file and builds a **US Letter** PDF: question text, then optional **embedded image** (if `image_relpath` points to an existing file), then options with **green** for correct and **red** for incorrect (supports multi-correct answers when stored as `A | B` in `correct_text`).

### Quick start

Run from the directory that contains both the database and the `quiz_media` folder (or adjust paths):

```bash
python export_quiz_pdf.py
```

Defaults: **`quiz_bank.sqlite`** → **`quiz_bank.pdf`**.

### Options

| Option | Description |
|--------|-------------|
| `--db PATH` | SQLite database |
| `--out PATH` | Output PDF |
| `--title TEXT` | Document title on the first page |
| `--media-base DIR` | Root for `image_relpath` values; default is the **parent directory of `--db`** |

If your DB lives somewhere other than next to `quiz_media`, set `--media-base` to the folder that contains `quiz_media` (or whatever directory prefixes the paths stored in `image_relpath`).

### Example

```bash
python export_quiz_pdf.py --db quiz_bank.sqlite --out review.pdf --title "Systems review"
```

---

## Typical workflow

1. `pip install -r requirements.txt`
2. `python quiz_scraper.py` (add `--cookies` if the site requires it)
3. `python export_quiz_pdf.py`

python export_quiz_pdf.py --out quiz_bank.pdf

Each run appends a full set of **`quiz_items`** rows for that scrape. Re-scraping the same question updates its **`question_bank`** row (and increments **`seen_count`**). If a question had no image on an earlier run, a later run can set **`image_relpath`** when a graphic is present (existing paths are kept when the new scrape has no image).
