# Other servers (SFTPGo / WebDAV)

Besides Nextcloud, NextPull can pull from a **SFTPGo** server (or any plain WebDAV server) that someone gave you access to. A job works exactly the same: choose the folder, the destination, Copy or
Move, a time and a speed limit. The transfers are done by [rclone](https://rclone.org) in both cases.

## Add a server

1. Open **Settings** and, in **Other servers (SFTPGo / WebDAV)**, press **Add a server**.
2. If you received an **invite code**, paste it and press **Use code**: the address (and the access key, if there is one) are filled in for you. Otherwise type the **server address**
   (for example `pull.example.com`) and press **Check**: NextPull tells you what it found.
3. Type your **user name** and **password**. If the administrator gave you an **access key**, open *Access key* and enter the header name and the key.
4. Press **Test and save**. NextPull signs in first; **nothing is saved if the test fails**. The password and key are stored encrypted for your Windows user and are never shown again.

The **Files** page and the job editor then have a **Connection** selector, so you can browse and pull from Nextcloud and from the server side by side.

## What is different from Nextcloud

| | Nextcloud | SFTPGo / WebDAV server |
| --- | --- | --- |
| Sign-in | in your browser, no password seen by NextPull | user name and password you were given |
| Delete after a verified copy (Move) | yes | yes, if the administrator allowed deleting files |
| Remove empty folders after a move | yes | not used (that login normally cannot remove folders). The folder of the job always stays |
| Quota shown in Settings | yes | no |
| Upload | never | never |

NextPull only **lists, downloads and (in Move mode, after the download was verified) deletes files**. It never uploads, renames or creates anything on the server.

## Security notes

- `http://` is accepted only for servers on your own network. For anything on the internet you must use `https://`: your password and key would otherwise be sent unencrypted. The server's certificate is always verified.
- If the server is hidden behind an **access key**, anyone without the key gets no answer at all, so NextPull cannot even detect the server type: use the invite code.
- Passwords and keys are never written to the logs or to a report. Reports replace the server, the user and the connection name with codes unless you tick *Include names*.

## Problems

| Message | Meaning |
| --- | --- |
| "The server rejected the saved login" | wrong or changed password. Edit the connection: remove it and add it again |
| "Cannot reach the server" | no internet, the server is down, or the access key is missing or wrong |
| "The server folder /x was not found" | the folder was renamed, or this account cannot see it |
| "Use https://..." | the address is not on your local network; use https |

For the person who runs the server, see [SFTPGo server setup](SFTPGo-Server-Setup).
