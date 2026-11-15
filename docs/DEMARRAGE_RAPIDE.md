# Démarrage rapide (français)

Guide pas à pas pour un PC Windows avec un compte **démo** MetaTrader 5.

> **À lire avant tout.** La plateforme démarre en mode **PAPER** (aucun ordre
> n'est envoyé au broker). Aucune stratégie n'est validée pour l'instant : la
> rentabilité doit être démontrée sur l'historique de ton broker, puis sur un
> compte démo. Ce logiciel n'est pas un conseil financier et le trading à effet
> de levier peut faire perdre de l'argent rapidement.

## 1. Ce qu'il faut

- Un **PC Windows** allumé pendant les heures de marché. Le module Python
  officiel de MetaTrader 5 n'existe que pour Windows : le terminal et le bot
  tournent donc sur le PC. Le Mac peut servir à lire le code et à lancer des
  backtests, pas à se connecter au terminal.
- Le terminal **MetaTrader 5** de ton broker, connecté à un compte **démo MT5**.
  La plateforme ne fonctionne qu'avec MetaTrader 5 (pas MetaTrader 4, pas une
  autre plateforme du broker). Dans le terminal, active le bouton
  *Algo Trading*.
- **Python 3.11 ou plus récent** (python.org). Pendant l'installation, coche
  « Add python.exe to PATH ».

## 2. Installer

1. Télécharge le projet : sur la page GitHub, bouton vert **Code > Download ZIP**,
   puis décompresse. (Ou : `git clone -b claude/mt5-autonomous-trading-sso0zi https://github.com/kelyanvidal5-ai/MT5-Trading-Bot.git`)
2. Double-clique sur **`setup_windows.bat`**. Il crée l'environnement Python,
   installe les dépendances, crée le fichier `.env` et lance un diagnostic.

Les scripts `.bat` n'ont pas pu être essayés sur un vrai Windows pendant le
développement. S'ils échouent, les commandes équivalentes sont :

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env
.venv\Scripts\python main.py check
```

Dans la suite, `python` veut dire `.venv\Scripts\python`.

## 3. Lire les informations du broker (rien à chercher à la main)

Le terminal doit être ouvert et connecté au compte démo.

```
python main.py symbols "*EUR*"
```

affiche les noms **exacts** des symboles chez ton broker (certains brokers
ajoutent un suffixe). Ensuite :

```
python main.py broker-info --symbols EURUSD GBPUSD USDJPY
```

affiche la devise du compte, le levier, les spécifications des contrats et les
**spreads réellement cotés**, puis propose un bloc à copier dans
`config/local.yaml` (crée ce fichier ; il n'est jamais envoyé sur GitHub).
Remplace les noms par ceux trouvés à l'étape précédente.

La commission par lot ne peut pas être lue automatiquement : regarde les
conditions de ton type de compte et renseigne
`costs: commission_per_lot_per_side:` dans `config/local.yaml`.

## 4. Télécharger l'historique (automatique)

Pas besoin d'exporter quoi que ce soit à la main :

```
python main.py download-history --symbols EURUSD GBPUSD --timeframe M5 --days 1095
```

Les fichiers arrivent dans `data/`. Si l'historique est court, dans le
terminal : *Outils > Options > Graphiques > Barres max dans le graphique* sur
« Unlimited », redémarre le terminal, ouvre un graphique M5 du symbole et
fais défiler vers la gauche pour forcer le téléchargement, puis relance.

## 5. Valider les stratégies sur ces données

```
python main.py validate --data EURUSD=data/EURUSD_M5.csv GBPUSD=data/GBPUSD_M5.csv --verbose
```

Le rapport est écrit dans `reports/`. Attends-toi à ce que la plupart des
stratégies soient **rejetées** : c'est le rôle de cette étape. Seules les
stratégies `validated` pourront trader en démo.

## 6. Lancer

Mode paper sur les vrais prix du terminal - dans `config/local.yaml` :

```yaml
mode: paper
paper:
  data_source: mt5
```

puis double-clique sur **`start_windows.bat`** (ou `python main.py run`) et
ouvre le tableau de bord : http://127.0.0.1:8050

Quand tu as au moins une stratégie `validated` et que le mode paper se comporte
comme prévu, passe à `mode: demo`. Le bot refuse de trader si le compte connecté
n'est pas un compte démo.

Arrêt d'urgence : bouton rouge du tableau de bord, ou **`stop_windows.bat`**.
Reprise : `python main.py resume --confirm`.

## 7. Notifications Discord

1. Dans ton serveur Discord : *Paramètres du salon > Intégrations > Webhooks >
   Nouveau webhook > Copier l'URL du webhook*.
2. Ouvre `.env` et colle l'URL :
   ```
   NOTIFY_WEBHOOK_URL=https://discord.com/api/webhooks/...
   ```
3. Redémarre le bot. Tu reçois les ouvertures de trades, les alertes de risque,
   les arrêts d'urgence et le rapport quotidien.

**L'URL du webhook est un secret** : quiconque la possède peut publier dans ton
salon. Ne la mets jamais sur GitHub ni dans une conversation. Pour qu'un ami
voie les notifications, invite-le simplement dans le salon Discord.

## 8. Partager le bot avec un ami

Le dépôt est public. Envoie-lui ce lien :

https://github.com/kelyanvidal5-ai/MT5-Trading-Bot/tree/claude/mt5-autonomous-trading-sso0zi

Il suit ce même guide sur son propre PC Windows. Règles importantes :

- **Chacun son compte et son fichier `.env`.** Ne partage jamais ton `.env`,
  tes identifiants MT5 ni ton URL de webhook.
- Chacun refait les étapes 3 à 5 avec **son** terminal : les symboles, les
  spreads et l'historique dépendent du broker et du type de compte, et une
  validation faite chez l'un ne vaut pas chez l'autre.
- Le bot reste en mode paper tant qu'il ne le change pas lui-même, et l'argent
  réel reste verrouillé (voir [LIVE_TRADING.md](LIVE_TRADING.md)).
- Dis-lui clairement que rien n'est prouvé rentable à ce stade
  (voir [STATUS.md](STATUS.md)).

## En cas de problème

- `python main.py check` : diagnostic de l'installation.
- `python main.py status` : état du moteur et des limites de risque.
- Les journaux sont dans `logs/` (les mots de passe et jetons y sont masqués).
- Symbole « NOT FOUND » : le nom n'est pas exact, relance `python main.py symbols`.
