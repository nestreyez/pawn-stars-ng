# Surveillance HDV NationsGlory → Discord

Surveille l'hôtel des ventes de **Ruby** via l'API publique officielle de NationsGlory et te @ sur Discord :

- 🔔 **Watchlist** : un item que tu suis est mis en vente moins cher qu'avant (ou sous ton prix max). Tu reçois le prix unitaire, la quantité, le lien et l'écart au prix habituel.
- 💰 **Bonnes affaires** : un item qui vaut au moins **2 000 $** l'unité est en vente au moins **30 % sous la médiane de ses 10 dernières ventes**.

Il tourne **gratuitement sur GitHub Actions** toutes les ~15 min. Ton PC n'a pas besoin d'être allumé.
(Il peut aussi tourner comme vrai bot Discord avec commandes `/watch` sur un serveur : voir tout en bas.)

---

## Mise en place sur GitHub (≈ 10 min)

### 1. Créer le webhook Discord
Sur ton serveur Discord, va dans les paramètres du salon d'alertes (⚙️), puis **Intégrations** > **Webhooks** > **Nouveau webhook**. Clique ensuite sur **Copier l'URL du webhook**.

Garde aussi ton identifiant Discord pour le @ : Paramètres > Avancés > active **Mode développeur**, puis clic droit sur ton pseudo > **Copier l'identifiant**.

### 2. Créer le dépôt GitHub
1. Sur github.com : **New repository**, nom par ex. `hdv-bot`, coche **Public**. Les Actions sont gratuites et illimitées pour un dépôt public. Ne coche rien d'autre.
2. Envoie les fichiers de ce dossier :
   - **Sans git** : sur la page du dépôt vide, clique **uploading an existing file** et glisse **le contenu** du dossier (pas le dossier lui-même), puis **Commit changes**.
     ⚠️ Vérifie que le fichier `.github/workflows/hdv.yml` est bien arrivé : les dossiers qui commencent par un point sont parfois masqués. S'il manque, fais **Add file > Create new file**, nomme-le `.github/workflows/hdv.yml` et colles-y le contenu du fichier.
   - **Avec git** :
     ```bash
     cd nationsglory-hdv-bot
     git init && git add . && git commit -m "Bot HDV"
     git branch -M main
     git remote add origin https://github.com/TON_PSEUDO/hdv-bot.git
     git push -u origin main
     ```

### 3. Ajouter les secrets
Dans le dépôt, va dans **Settings** > **Secrets and variables** > **Actions** > **New repository secret**, et crée :

| Nom | Valeur |
|---|---|
| `DISCORD_WEBHOOK_URL` | l'URL du webhook (étape 1) |
| `DISCORD_USER_ID` | ton identifiant Discord (pour le @) |
| `NG_API_KEY` | ta clé `ngp_…` de la page Développeurs de NationsGlory (facultatif si l'API répond sans clé) |

Les secrets ne sont jamais visibles publiquement, même si le dépôt est public. **Ne mets jamais de secret dans `config.yaml`.**

### 4. Diagnostic puis premier passage
1. Onglet **Actions**. Si GitHub demande d'activer les workflows, accepte.
2. Clique **Surveillance HDV** > **Run workflow** > mode **probe** > **Run workflow**. Ouvre le run, puis l'étape « Diagnostic de l'API ». Les lignes `→ compris : …` doivent montrer le bon nom, le bon prix et la bonne quantité.
   Si quelque chose est vide ou faux, copie-moi ce log.
3. Relance **Run workflow** en mode **scan**. Tu dois recevoir sur Discord « ✅ Surveillance HDV démarrée » avec les prix actuels de ta watchlist.

Ensuite ça tourne tout seul toutes les ~15 min.

---

## Au quotidien

- **Ajouter / retirer un item** : sur GitHub, ouvre `config.yaml`, clique le crayon ✏️, modifie la section `watchlist`, puis **Commit changes**. C'est pris en compte au passage suivant.
  ```yaml
  watchlist:
    - item: "2535-8"          # ID comme dans l'URL du site, ou l'URL complète
      max_price: 1500         # facultatif : ne te prévenir que sous ce prix unitaire
    - item: "https://nationsglory.fr/servers/ruby/hdv/10162-0"
  ```
- **Régler la détection des affaires** : section `deals:` de `config.yaml` (valeur minimum, décote, nombre de ventes de référence…).
- **Forcer un passage** : Actions > Surveillance HDV > Run workflow > scan.

## Ce qu'il faut savoir

- **Délai** : l'API met à jour les ventes toutes les ~15 min et les annonces toutes les ~30 min, et GitHub lance les passages planifiés avec parfois quelques minutes de retard. Une très bonne affaire peut déjà être partie à la réception du ping.
- **60 jours sans commit** : GitHub suspend les tâches planifiées d'un dépôt public inactif. Le bot t'envoie un rappel sur Discord à partir de 50 jours. Un simple commit suffit, par exemple modifier la watchlist. Si c'est déjà suspendu : Actions > Surveillance HDV > **Enable workflow**.
- **API en panne** : après 3 passages en échec d'affilée, tu reçois un avertissement, puis un message quand ça revient.
- **Historique** : les ventes sont gardées entre deux passages dans le cache GitHub (dossier `state/`). Le premier jour, le bot complète l'historique de 25 items par passage, les plus chers d'abord. Si ce cache est perdu, l'historique repart de zéro et tu reçois à nouveau « Surveillance démarrée ».
- **Dépôt public** : le code et ta watchlist sont visibles par tous. Tes secrets ne le sont pas.

## Comment les bonnes affaires sont détectées

- **Référence** = médiane des 10 dernières ventes de moins de 30 jours.
- Les **transferts entre comptes** sont ignorés : ventes à moins de 20 % ou plus de 5× la médiane, et rafales de ventes identiques dans la même heure (ex. 40 ventes à 0,10 $).
- Il faut au moins 5 ventes connues. Sinon, le bot utilise le cours fourni par l'API s'il existe (indiqué dans l'alerte).
- Alerte si référence ≥ 2 000 $ **et** prix ≤ référence − 30 %. Tous les prix sont **unitaires**.
- Pas de spam : une même annonce ne ping qu'une fois. Nouveau ping si le prix baisse encore de 5 %, ou si l'affaire disparaît puis revient.

---

## Option : vrai bot Discord sur un serveur allumé 24h/24

Si un jour tu as un VPS ou un Raspberry Pi, le même code tourne comme bot Discord avec les commandes `/watch add|remove|list`, `/prix`, `/deals` et `/scan`, et une vérification toutes les 5 min.

1. Portail développeur Discord : **New Application** > **Bot** > **Reset Token**. Dans **OAuth2 > URL Generator**, coche `bot` et `applications.commands`, avec les permissions *Send Messages* et *Embed Links*, puis invite le bot.
2. Dans `config.yaml`, mets `channel_id` et `guild_id`.
3. Lance :
   ```bash
   pip install -r requirements.txt
   export DISCORD_TOKEN="…" DISCORD_USER_ID="…" NG_API_KEY="…"
   python -m hdvbot
   ```

Autres commandes utiles en local : `python -m hdvbot probe -i 2535-8` (diagnostic) et `python -m hdvbot once` (un passage affiché dans le terminal).

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest
```

Les tests utilisent une fausse API et un faux webhook locaux.
