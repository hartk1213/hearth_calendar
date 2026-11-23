# Installing Hearth on Unraid

This takes about 10 minutes. You'll fill in a form in Unraid's Docker tab, then do everything else on screen in Hearth.

## 1. Add the Hearth template

Open the Unraid terminal (the **>_** icon at the top right), paste this line and press Enter:

```bash
wget -qO /boot/config/plugins/dockerMan/templates-user/my-hearth.xml https://raw.githubusercontent.com/hartk1213/hearth_calendar/main/unraid/hearth.xml
```

Then close the terminal.

## 2. Create the container

1. Go to the **Docker** tab, click **Add Container**, and choose **hearth** from the **Template** list.
2. Fill in the form:

| Field | What to enter |
|---|---|
| **Web page port** | `8095`, or any port no other app uses |
| **Time zone** | Yours, e.g. `America/Los_Angeles` |
| **Database host** | Your Unraid server's IP (e.g. `192.168.1.10`) if you have a MariaDB container. Leave it **empty** to store data in `appdata/hearth` instead. |
| **Database password for Hearth** | Make up a password |
| **MariaDB root password** | Your MariaDB root password, so Hearth can create its database on the first start |

3. Click **Apply**.

To find your server's IP: it's the address you type to open the Unraid web page. The MariaDB row in the Docker tab also shows it, like `192.168.1.10:3306`.

## 3. Check it started

Click the Hearth icon and choose **Logs**. You should see:

```
[hearth] created database 'hearth' and user 'hearth'. You can now remove DB_ROOT_PASSWORD ...
Hearth 1.0.0 is running at http://localhost:8080
```

Then click the Hearth icon, choose **Edit**, **clear the MariaDB root password field**, and click **Apply**. Hearth doesn't need it again.

**If the log says the admin login was refused:** many MariaDB containers only let root log in from inside their own container. Create the database from there instead. Open the terminal, paste these two lines one at a time, and replace `MariaDB` if your container has a different name:

```bash
HP=$(docker inspect hearth --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^DB_PASSWORD=//p')
```

```bash
printf "CREATE DATABASE IF NOT EXISTS hearth CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;\nCREATE USER IF NOT EXISTS 'hearth'@'%%' IDENTIFIED BY '%s';\nALTER USER 'hearth'@'%%' IDENTIFIED BY '%s';\nGRANT ALL PRIVILEGES ON hearth.* TO 'hearth'@'%%';\nFLUSH PRIVILEGES;\nSELECT 'Hearth database is ready' AS result;\n" "$HP" "$HP" | docker exec -i MariaDB sh -c 'mariadb -uroot -p"$MYSQL_ROOT_PASSWORD"'
```

When it prints **Hearth database is ready**, clear the root password field in Hearth's settings, click **Apply**, and check the logs again.

## 4. Set it up on screen

Open `http://<your-server-ip>:8095` (or click **WebUI** on the Hearth icon). A short setup walks you through naming your calendar, adding your family, linking calendars and choosing your town for the weather.

## Updating

When the Docker tab shows **apply update** next to Hearth, click it. Your data is kept.

## Backups

- **With MariaDB:** everything is in the `hearth` database, so your usual MariaDB backups cover it.
- **Without a database:** back up `/mnt/user/appdata/hearth` (the Appdata Backup plugin does this for you).

## Troubleshooting

| Log says | Fix |
|---|---|
| `waiting for database at ...` | **Database host** must be the server's IP (not `localhost`), and MariaDB must publish port 3306. |
| `port is already allocated` | Change **Web page port** to another number. |
| `Database login failed` | The Hearth password doesn't match the database user. Fill in the root password again and click **Apply**, so Hearth can reset it. |
| Calendar times off by hours | Set **Time zone** correctly. |
