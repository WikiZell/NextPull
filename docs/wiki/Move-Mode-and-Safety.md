# Move mode and safety

## Copy vs Move

- **Copy**: files are downloaded; Nextcloud is untouched.
- **Move**: each file is downloaded, **verified**, and only then deleted on Nextcloud. This is rclone's own *move* behaviour; NextPull adds no extra deletion logic.

Files deleted through Nextcloud go to that account's **Nextcloud trash** and use space there until the trash is emptied (Nextcloud setting or `occ trashbin:cleanup`).

## Dry run

A job with **Always dry run** ticked changes nothing, neither on Nextcloud nor on your PC. The run lists what it *would* transfer (history label *Would transfer*). Always do this once with a new job.

## Guarantees

- **Nothing is overwritten**: downloads are written to a `.partial` file and renamed when complete.
- **Interrupted runs resume** on the next run; half-written files are never presented as finished.
- **Name clashes are skipped, not merged.** Windows is case-insensitive and cannot hold some names that Nextcloud can (for example two files that differ only by upper/lower case, or names with forbidden characters). NextPull checks the names before the run and skips the clashing ones (all of them), recording each as *Skipped* with the reason. In Move mode, nothing is deleted for a skipped file.
- **The pre-check**: before rclone starts, NextPull checks that the Nextcloud folder exists and the login works, and gives a plain-language message if not.
- **A failed delete is not a failed download.** If a file was downloaded but Nextcloud refused to delete it (HTTP 403), the run is *Needs attention* and the file shows a "not deleted on Nextcloud" note.

## Why a delete can fail (403)

Nextcloud can only delete a file if the file itself is writable by the Nextcloud web user (`www-data`). Files that another program created as `root:root` with mode `0644` download fine but cannot be deleted through Nextcloud. rclone then also keeps the parent folders ("not deleting directories as there were IO errors"). The fix is on the server: make the files writable by the web user (for example mode `666` for files and `777` for folders, or the correct owner). NextPull cannot work around it.

## Your login

See [Security and privacy](Security-and-Privacy).
