# Tsundere RSS Bot

Bot Telegram qui surveille le flux RSS de tsundere.to et publie automatiquement
les épisodes HARDSUB dans un canal. Conçu pour tourner sur **Google Colab**.

## Fonctionnement

1. Lit le flux RSS à intervalle régulier (`CHECK_INTERVAL`).
2. Ne garde que les entrées `tsundere_hardsub = true` avec un lien Transfer.it.
3. Résout Transfer.it → serveurs Mega, teste chaque serveur (HTTP 200/206).
4. Télécharge le fichier (aria2), vérifie la taille (< 1,95 Go).
5. Upload via TGUP (MTProto) si présent, sinon via l'API Bot locale.
6. Anti-doublon via SQLite.

## Commandes (réservées au créateur)

| Commande  | Rôle                                    |
|-----------|-----------------------------------------|
| `/status` | Stats du bot (PV uniquement)            |
| `/cancel` | Annule le téléchargement Transfer.it    |
| `/test`   | Retraite les 3 derniers épisodes du RSS |

## Lancer sur Colab

1. Ouvre `run_colab.ipynb` dans Colab.
2. Remplace l'URL du repo, remplis le `.env` (cellule 2).
3. Exécute les cellules dans l'ordre. `colab_setup.sh` installe aria2,
   compile et lance `telegram-bot-api`, puis `python bot.py` démarre le bot.

`TELEGRAM_API_ID` / `TELEGRAM_API_HASH` s'obtiennent sur https://my.telegram.org.

⚠️ Colab coupe les sessions inactives et efface le disque à la déconnexion :
la base `rss_bot.db` (anti-doublon) est alors perdue. Pour la garder, monte
Google Drive et mets `DB_FILE=/content/drive/MyDrive/rss_bot.db`.

## Variables d'environnement

Voir `.env.example`.

## Sécurité

Ne commit jamais `.env`, les `.session` ni la base `.db`.
