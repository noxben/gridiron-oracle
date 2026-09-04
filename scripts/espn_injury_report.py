# ---------------------------------------------------------------------------
# Step 3 — Injury overlay
# ---------------------------------------------------------------------------

def fetch_injury_report(season: int) -> dict:
    """
    Fetch current-season player injury/health status from ESPN Fantasy.

    Returns:
        {
            gsis_id: {
                "play_probability": float,
                "injury_detail": str
            }
        }

    ESPN is used here instead of nflreadpy.load_injuries() because the
    nflreadpy injury dataset is not current for the 2026 season.

    The ESPN Fantasy API returns current player health data through
    the kona_player_info view. ESPN IDs are mapped back to GSIS IDs using
    the existing nflreadpy player-ID crosswalk.
    """

    log.info("Fetching current 2026 injury data from ESPN Fantasy API...")

    load_dotenv(ENV_PATH)

    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not espn_s2 or not swid:
        log.error(f"ESPN_S2 and SWID not found in {ENV_PATH}")
        sys.exit(1)

    # -----------------------------------------------------------------------
    # ESPN player pool
    #
    # This is the same endpoint successfully used by fetch_adp_stg.py.
    # Do NOT use fantasy.espn.com here. It can return an HTML redirect/403.
    # -----------------------------------------------------------------------

    url = (
        f"https://lm-api-reads.fantasy.espn.com/"
        f"apis/v3/games/ffl/seasons/{season}/"
        f"segments/0/leaguedefaults/3"
    )

    headers = {
        "Cookie": f"espn_s2={espn_s2.strip()}; SWID={swid.strip()}",
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    }

    try:
        response = requests.get(
            url,
            params={"view": "kona_player_info"},
            headers=headers,
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

    except requests.RequestException as e:
        log.error(f"ESPN injury fetch failed: {e}")
        sys.exit(1)

    except ValueError as e:
        log.error(f"ESPN returned invalid JSON: {e}")
        sys.exit(1)

    players = data.get("players", [])

    if not players:
        log.error("ESPN injury fetch returned zero players")
        sys.exit(1)

    log.info(f"ESPN player pool: {len(players)} players")

    # -----------------------------------------------------------------------
    # Existing crosswalk
    #
    # fetch_id_crosswalk() already gives us:
    #   gsis_id
    #   espn_id
    #   pfr_id
    #   name
    #   position
    #   team
    # -----------------------------------------------------------------------

    ids = fetch_id_crosswalk()

    espn_to_gsis = {}

    for _, row in ids.iterrows():
        espn_id = row.get("espn_id")
        gsis_id = row.get("gsis_id")

        if pd.isna(espn_id) or pd.isna(gsis_id):
            continue

        espn_to_gsis[str(int(espn_id))] = str(gsis_id).strip()

    log.info(f"ID crosswalk: {len(espn_to_gsis)} ESPN → GSIS mappings")

    # -----------------------------------------------------------------------
    # ESPN injury status → Gridiron Oracle probability
    # -----------------------------------------------------------------------

    result = {}
    flagged = 0
    unmatched = 0

    for entry in players:

        player = entry.get("player", entry)

        espn_id = player.get("id") or entry.get("id")

        if not espn_id:
            continue

        espn_id = str(espn_id)

        gsis_id = espn_to_gsis.get(espn_id)

        if not gsis_id:
            unmatched += 1
            continue

        status = str(
            player.get("injuryStatus")
            or player.get("status")
            or "ACTIVE"
        ).strip().upper()

        injury_detail = str(
            player.get("injuryStatus")
            or ""
        ).strip()

        # ESPN's injuryStatus is the important field here.
        #
        # If ESPN reports ACTIVE, there is no injury overlay to apply.
        # We still include it so an old injury state gets cleared.
        play_prob = INJURY_STATUS_MAP.get(status, 1.0)

        # ESPN may have no useful injury status but still provide an
        # injury-related field. Keep the detail conservative.
        if not injury_detail:
            injury_detail = "Active" if play_prob == 1.0 else status

        result[gsis_id] = {
            "play_probability": play_prob,
            "injury_detail": injury_detail,
        }

        if play_prob < 1.0:
            flagged += 1

    log.info(
        f"ESPN injury data: {len(result)} players mapped | "
        f"{flagged} currently flagged | "
        f"{unmatched} ESPN players without GSIS mapping"
    )

    return result