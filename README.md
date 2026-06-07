# Bulk Image Downloader
python .\download.py
python .\download.py --retry-errors
Small Python downloader for episode-based numbered image URLs.

## Setup

Edit `config.json` for titles and frequently changed options. Less frequently changed defaults live in `unlessConfig.json`.
Both files support `//` comments after each setting. Values in `config.json` override values with the same key in `unlessConfig.json`.

Title list mode:

```json
{
  "url_prefix": "https://tupa.zerobywai.com/manhua/",
  "url_suffix": [
    { "name": "", "url": "l1h1u8dn8ad/", "special_episode": ["6.5", "番外"], "probable_episode": [1200] },
    { "name": "custom-folder", "url": "https://tupa.zerobywai.com/manhua/na5n5va6h/" }
  ],
  "start_episode": 2,
  "max_episode": null,
  "probable_episode": [600, 700],
  "start_number": 1,
  "zero_padding": 3,
  "extension": ["jpg", "png"],
  "image_workers": 2
}
```

`url_suffix` must be an array. Each item is an object:

```json
{
  "name": "",
  "url": "l1h1u8dn8ad/",
  "special_episode": ["6.5", "番外"]
}
```

`url` can be a suffix or a complete URL. If it is a suffix, it is combined with `url_prefix`. If it is a complete URL, `url_prefix` is ignored for that item.

`name` is the folder name under `output_dir`. If `name` is empty, the folder name comes from the last URL path segment.

`special_episode` is optional. It can be omitted, `null`, or an empty array. When it has values, those episodes run before the normal integer episode flow. Values are treated as text, so use strings such as `"6.5"` or `"番外"`. Files are saved into folders such as `download/title/6.5/001.jpg`.

Each `url_suffix` item can also have its own `probable_episode` array. These title-specific jump points are merged with the top-level `probable_episode`, then sorted and deduplicated.

The downloader creates URLs like:

```text
https://tupa.zerobywai.com/manhua/l1h1u8dn8ad/2/001.jpg
https://tupa.zerobywai.com/manhua/l1h1u8dn8ad/2/002.jpg
...
https://tupa.zerobywai.com/manhua/l1h1u8dn8ad/3/001.jpg
```

`zero_padding` is the maximum local filename width and the first URL format to try. For example, when `zero_padding` is `3`, image number `1` tries `001`, then `1`, then `01`. No matter which source filename succeeds, the local file is saved with the maximum padded name, such as `001.jpg`.

`start_number` can be `0` or greater. When `start_number` is `0` and `zero_padding` is `3`, image number `0` tries `000`, then `0`, then `00`.
If `start_number` is `0`, image number `0` is treated as optional during episode verification; a chapter that starts at `001.jpg` is still considered valid. If `start_number` is `1`, missing `001.jpg` is still reported as an error.

`max_episode` accepts only an integer, `null`, or an empty string. Use `10`, not `"10"`.

`probable_episode` is an array of common possible episode jump points, such as `[600, 700]`. It is used only when `max_episode` is `null` or `""`. When automatic episode detection reaches `consecutive_missing_episode_limit`, the downloader jumps to the next probable episode greater than the current episode and keeps detecting. If `max_episode` is an integer, both common and title-specific `probable_episode` values are ignored.

When an episode is reached through `probable_episode`, the downloader first checks whether that episode folder already has at least one valid downloaded image. If it does, that jump point is skipped and the downloader moves to the next `probable_episode`. This fast skip only applies to jump-point episodes, not to episodes reached through the normal `1, 2, 3...` flow.

`extension` can be a string or an array. When it is an array, the downloader tries extensions in order for each candidate image number. For example, `["jpg", "png"]` with `zero_padding: 3` tries `001.jpg`, `001.png`, `1.jpg`, `1.png`, `01.jpg`, then `01.png`. The image number only counts as missing if every candidate returns 404.

Files are always saved into title folders under `output_dir`:

```text
https://tupa.zerobywai.com/manhua/na5n5va6h/9/001.jpg
-> download/na5n5va6h/9/001.jpg
```

`start_episode` and `max_episode` apply to every title in `url_suffix`.
Each episode stops when the downloader sees `consecutive_404_limit` consecutive 404 responses.
When `max_episode` is `null` or `""`, episodes stop when the downloader sees `consecutive_missing_episode_limit` missing episodes in a row.
If `probable_episode` still has a later jump point, the downloader uses that jump point before stopping.
404 is treated as the end signal and is not written to `error.md`.
Non-404 errors are retried. If they still fail, they are written to root `error.md` and the downloader continues the overall flow.
At the start of a normal download run, the previous root `error.md` is removed. If the run finishes with no recorded errors, no `error.md` is left behind.
`consecutive_error_limit` controls how many consecutive non-404 errors can happen in an episode before that episode is skipped.
`all_retry` controls how many full retry rounds are attempted from retryable `error.md` records after the main flow finishes.
When `verify_downloads` is enabled, downloaded images are checked for basic corruption, missing numbers, duplicate numbers, extra numbers, and whether the file count matches the last filename number. JPEG validation is intentionally tolerant: a non-empty file with a JPEG header is accepted even if the final JPEG end marker is missing, because browsers can usually open those files. That case is written to `error.md` as a `NOTICE-*` record, not an error, and `all_retry` will not remove it.

## Usage

Preview the generated URLs:

```powershell
python download.py --dry-run
```

Download files:

```powershell
python download.py
```

Check existing folders without downloading:

```powershell
python download.py --check-only
```

Retry only retryable records from root `error.md` without rerunning the main download flow:

```powershell
python download.py --retry-errors
```

This mode does not read or validate `url_suffix`; it only uses download-related settings such as `timeout_seconds`, `retries`, `request_jitter`, and `verify_downloads`.

Use a different config file:

```powershell
python download.py --config another-config.json
```

Only download content that you have permission to access and store.
