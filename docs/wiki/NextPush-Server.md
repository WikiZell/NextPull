# NextPush: a simple server for NextPull

**[NextPush](https://github.com/WikiZell/NextPush)** is our own small file server, made to be the other end of NextPull. It runs on Linux or Windows (also headless), is managed from a web page, and needs no configuration files.
The administrator creates users, chooses which server folders each user may use, and sends an **invite code**. You paste the code in NextPull and type the password: that is all.

You do not need NextPush to use NextPull (Nextcloud and any SFTPGo / WebDAV server work too), but it makes sharing folders with a friend or family much easier.

## If someone gave you an invite code

1. Make sure you run NextPull **0.2.0 or later** (*Settings -> Updates* shows your version).
2. **Settings -> Other servers -> Add a server**, paste the **invite code**, press **Use code**.
3. Type the **password** you received (it is never inside the code, so it was sent separately) and press **Test and save**.
   NextPull signs in first and shows the encryption it found (for example "Encrypted: TLSv1.3, certificate from Let's Encrypt verified"). **Nothing is saved if the test fails.**
4. Create a job (**Jobs -> New job**): in *Connect to* choose the server, browse to a folder, pick the destination on your PC, **Copy** or **Move**, and the time. Try *Always dry run* first.

*Move* deletes a file on the server only after rclone verified the copy, so the administrator gives you the mode "Download and delete after copy". If you only have "Download only", Move still downloads, but the files are not
deleted and the run says so.

## What the administrator can limit for you

Which folders you see (you never see real server paths), whether you may delete after copying, a speed limit, how many connections at once, and an end date for your access. Nobody can upload, rename or create folders on the server.

## If something does not work

| Message in NextPull | Meaning |
| --- | --- |
| "Cannot reach the server" | no internet, the server is off, or the **access key** inside your invite is old (the administrator made a new one: ask for a new invite) |
| "The server rejected the saved login" | wrong or changed password: remove the connection and add it again with the new password |
| "The server folder /x was not found" | the folder was renamed, or your account does not see it |
| a run says files were downloaded but "not deleted" | your account may only download; ask the administrator for "Download and delete after copy" |

## Setting up the server (administrator)

See the NextPush repository: [README](https://github.com/WikiZell/NextPush#readme), [Deployment](https://github.com/WikiZell/NextPush/blob/main/docs/DEPLOYMENT.md) (services, Docker, reverse proxy) and
[Security](https://github.com/WikiZell/NextPush/blob/main/docs/SECURITY.md). If you use a different SFTPGo setup, see [SFTPGo server setup](SFTPGo-Server-Setup).
