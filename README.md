# La Liga dashboard

One script rebuilds the whole dashboard from public data and writes it as a single self-contained web page.
The page has six tabs:

- **Table.** The standings with expected goals (xG), expected points and the last five results, for all games,
  home or away games, or the last 5 or 10. Below it, a points race or a position-by-matchday chart for any clubs you pick.
- **Team stats.** All 20 clubs plotted on any two of about 40 statistics (chances created against chances allowed,
  points against expected points, results against what the betting market expected, shot volume against shot quality,
  half-time against full-time, discipline and so on), with presets, a Random button and a sortable table.
  Clubs are drawn in their own colours.
- **Clubs.** One club at a time: the stats it leads the league in, match-by-match xG for and against, points against
  expected points, where it ranks on 15 measures, every result, and the next fixtures with the model’s chances.
- **Players.** Every shot a player has taken this season, on a pitch: a shot map (coloured by result or by chance quality),
  a zone heat map coloured by conversion, goals against xG or how often they shoot from each zone compared with the
  league, and a smooth shot heat map. Filters for open play, set pieces and penalties, feet or headers, home or away and
  the last 5 or 10 games. A player card with goals, xG, assists, expected assists, shots, key passes, minutes and per-90
  figures, and league-rank badges. Put any two players side by side, see where every player sits on shot selection
  against finishing, find the players who shoot most like the one you picked, and browse ten league leaderboards.
  Each club page also gets a squad table.
- **Matches.** A matchday browser with score, half-time score, xG, shots, corners, cards and pre-match market odds,
  plus the biggest upsets, wins against the run of play and a full results grid.
- **Projections.** 20,000 simulations of the rest of the season: title, top four, top six and relegation chances,
  a finishing-position grid, each club’s range of final points, and a test of how the same model did on past seasons.

Match data comes from [football-data.co.uk](https://www.football-data.co.uk/spainm.php) (results, half-time scores,
shots, corners, fouls, cards, xG and pre-match odds). Matchday numbers and the remaining fixtures come from
[openfootball](https://github.com/openfootball/football.json). Player data comes from [Understat](https://understat.com/league/La_liga):
every shot with its position on the pitch and its xG, and each player's minutes, assists, expected assists and key passes
in every game. ESPN's public match feeds add, for every game, the lineups and substitution times (which give minutes
played), shots on target, fouls committed and suffered, offsides, cards and goalkeeper saves, and they supply a result
the night a game ends, before football-data.co.uk posts it. Nothing needs an account or API key.

The heat maps are built from shots. Free sources do not publish where players touch the ball, so a touch heat map
(the kind TV graphics show) is not possible from public data.

## Run it on your computer

```
pip install -r requirements.txt
python laliga_build.py --raw raw --out docs/index.html
```

The first run downloads five seasons of match files from football-data.co.uk (well under 1 MB), one file per game from
Understat (about 1,900 games) and one per game from ESPN for the two most recent seasons (about 500 so far), politely
spaced out, which takes 20 to 30 minutes. Everything is kept in `raw/`, so later runs only fetch the games played
since, and take a minute or two. Open `docs/index.html` in a browser.

Options: `--espn-seasons 5` fetches ESPN for all five seasons (minutes for every season, about 15 more minutes on the
first run); `--no-espn` skips ESPN; `--no-understat` skips Understat; `--no-players` leaves player data out altogether.

`python check_build.py docs/index.html` runs the same sanity checks the automation uses.

If football-data.co.uk is unreachable for a finished season, the script falls back to a copy on GitHub that has
results and match stats but no xG or odds; those columns are then hidden for that season.

If Understat is unreachable, seasons up to 2024-25 fall back to an archived copy of Understat's shots on GitHub
(about 27 MB, downloaded once), with minutes, games and cards from a GitHub mirror of Understat's season totals
(7 MB, once). If ESPN is reachable, minutes come from there too. If
Understat is down and a season is not in the archive, the Players tab still works from ESPN alone (goals, assists,
shots, minutes, cards, saves) but without shot maps or xG. If ESPN is down, nothing is lost except same-night results
and the match-sheet stats. In every case the rest of the page still updates, and the checker prints a note instead of
failing.

ESPN's feeds are the ones its own site uses; they are public but not documented or guaranteed, so if ESPN changes them
the build log will say so and the dashboard carries on without them.

## Update it automatically every day (GitHub Pages)

This publishes the dashboard as its own website at a `github.io` address and rebuilds it every morning.

**Before you start.** You need a free GitHub account. On a free account GitHub Pages works only for public
repositories, so the repository and the site will both be public. That is fine here: they contain only code and public data.

1. **Create the repository and upload the files.** Unzip this folder and create a new public repository. Upload
   **every file** in the `laliga-dashboard` folder, keeping the folder structure, to the top level of the repository,
   so that `laliga_build.py` and `.github/workflows/update.yml` sit at the root, not inside a subfolder.
   Six files are required: `laliga_build.py`, `check_build.py`, `template.html`, `requirements.txt`,
   `static/teams.json` and `.github/workflows/update.yml`. `README.md` is optional. Do not create `raw/` or `docs/`;
   the script makes them.
   - `.github` is a hidden folder and is easy to miss. On a Mac press Cmd+Shift+. in Finder to show hidden files; on
     Windows choose View > Show > Hidden items. Or skip the problem: in GitHub choose Add file > Create new file, type
     `.github/workflows/update.yml` as the file name, and paste in the contents of that file.
2. **Turn on Pages.** In the repository open **Settings > Pages** and set **Source** to **GitHub Actions**.
3. **Run it once.** Open the **Actions** tab, choose **Update La Liga dashboard**, and click **Run workflow**
   (if GitHub asks you to enable workflows first, do that). The first run takes 15 to 25 minutes because it downloads
   every game from Understat once; after that a run takes a minute or two.
4. **Open the site.** When both jobs show a green check, the address is on **Settings > Pages**, and it looks like
   `https://<your-name>.github.io/<repository>/`.

No passwords, tokens or secrets are needed.

After that it rebuilds itself every morning at 07:00 UTC. To change the time, edit the `cron` line in
`.github/workflows/update.yml`. The checker runs before publishing, so a bad download never replaces a good page.
If a run fails, the previous version stays up; open the run in the Actions tab and click the red step to see why.

**One GitHub rule to know about.** In a public repository, GitHub switches off scheduled jobs after 60 days with no
activity in the repository, and this job does not commit anything itself. If the daily updates stop, open the Actions
tab and click **Enable workflow**. Any small edit to the repository also counts as activity.

football-data.co.uk usually updates twice a week (after the weekend games and after midweek games), so a game
played in the last day or two may not be on the page yet. Running the workflow again later picks it up.

## What updates itself, and what you maintain by hand

Everything about the current season updates itself: results, the table, all team statistics, player shots and
stats, lineups and minutes, the projections, the fixture list and the backtest. A game played last night shows up
the next morning with ESPN's stats; its xG and odds arrive when football-data.co.uk posts the game, usually within
two or three days. The season rolls over on its own: from August the script looks for the new
season, and until its first games are in the data it keeps showing the last finished season.

One file is yours to edit:

| File | What it holds | When to change it |
|---|---|---|
| `static/players_seed/<season>.json` | Hand-collected season totals (goals, assists, games, penalties, goalkeepers' goals conceded) for seasons where neither Understat nor ESPN could be reached. The files for 2025-26 and 2026-27 were filled in from ESPN's leaderboards and published season tables on 3 November 2026. | Normally never: a seed file is used only when a season has no other player data, so the GitHub version replaces it automatically. Delete a file if you want that season to show nothing rather than leaderboards. |
| `static/teams.json` | Each club’s display name, three-letter code and colours, keyed by the name football-data.co.uk uses, plus the names openfootball uses under `of` and Understat uses under `us`. ESPN's names are matched automatically against all of these, ignoring accents and words like CF and Real. | When a promoted club is not already in it. The build log names any club it does not recognise (it still works, with a grey default colour), and any openfootball, Understat or ESPN name it cannot match, which is what you add under `of` or `us`. |

## How the projections work

Each club gets an attack and a defence rating fitted to every league game so far, using 60% xG and 40% goals,
adjusted for opponent strength and home advantage. Ratings start from last season’s (pulled 40% back toward average;
promoted clubs start where last season’s relegated clubs finished) and are pulled toward that start as if each club had
also scored and conceded 8 goals at that level. Each simulated season draws its own version of every rating, to allow
for how uncertain they still are, then plays out the remaining games with Poisson-distributed goals. Ties are split by
goal difference (La Liga uses head-to-head first).

The weights are constants at the top of `laliga_build.py` (`W_XG`, `PRIOR_GOALS`, `DRIFT`). The Projections tab
shows how the model did when fitted at the same stage of each past season in the data, compared with simply scaling
up points per game, so you can see whether a change helps.

## Good to know

- xG is football-data.co.uk’s figure; it will not match Opta, Understat or FBref exactly. Expected points treat each
  side’s xG as the average of a Poisson distribution, which is a common approximation.
- Market probabilities are the bookmaker’s pre-match odds with its margin removed.
- The league table uses La Liga’s tie-breaks: head-to-head once both meetings between the tied clubs have been
  played, then goal difference, then goals scored.
- Minutes played are worked out from lineups and substitution clocks, so they are whole minutes without stoppage time
  and can differ by a minute or two from other sites. Players are matched between Understat and ESPN by name within
  the same club; a player whose names differ too much between the two appears twice, once from each source.
- Player xG is Understat's, and team xG is football-data.co.uk's, so a club's total in the Players tab can differ a
  little from the team pages. Own goals are left out of player shots.
- Similar shooters compares where players shoot from, how often they head it and how often their shots come from set
  pieces, using non-penalty shots of players with at least 20.
- Understat is a free site run by volunteers. The script waits between requests and only fetches each finished game
  once; please keep it that way.
- The page is about 2 MB with five seasons of player data, and works on phones. The only thing it fetches from the internet is the Saira Condensed
  and Instrument Sans typefaces from Google Fonts; if that is blocked or you are offline, it falls back to system fonts.

## Files

```
laliga_build.py       the whole build: downloads data, builds player data, fits the model, writes the page
template.html         the page itself (layout, styles and the scripts that draw everything)
check_build.py        checks a built page before it is published
static/teams.json     club names, codes and colours
.github/workflows/    the daily GitHub Actions job
requirements.txt      pandas, numpy, pyreadr (pyreadr only reads the archived shots)
```
