#!/bin/sh
# The made-up home of the screenshots (run inside sandbox.sh): jobs, files, one sync.
set -e
for d in "$HOME" /mnt/backup-disk /srv; do find "$d" -mindepth 1 -delete; done


L="$HOME/Documents"; R="/mnt/backup-disk/Documents"
mk() { mkdir -p "$(dirname "$1")"; printf '%s\n' "$2" > "$1"; touch -d "$3" "$1"; }
for side in "$L" "$R"; do
  mk "$side/Recipes/bread.md" "flour, water, salt" "2026-03-02 10:00"
  mk "$side/Recipes/soup.md" "carrots" "2026-03-04 18:20"
  mk "$side/Taxes/2025/receipts.pdf" "%PDF placeholder" "2026-02-11 09:15"
  mk "$side/Taxes/2025/summary.ods" "summary" "2026-02-12 21:40"
  mk "$side/Notes/garden.txt" "tomatoes, basil" "2026-04-20 07:30"
  mk "$side/Notes/reading-list.txt" "a book" "2026-05-01 22:00"
  mk "$side/Projects/birdhouse/plan.svg" "<svg/>" "2026-06-14 16:05"
  mk "$side/Projects/birdhouse/cut-list.txt" "6 boards" "2026-06-14 16:10"
  mk "$side/Projects/bike/parts.csv" "chain,tyres" "2026-07-03 12:00"
  mk "$side/todo.txt" "call the plumber" "2026-08-01 08:00"
done
mkdir -p "$XDG_CONFIG_HOME/mysync/conf.d"
cat > "$XDG_CONFIG_HOME/mysync/conf.d/documents.toml" <<TOML
left = "~/Documents"
right = "/mnt/backup-disk/Documents"
exclude = ["*.tmp"]
TOML
cat > "$XDG_CONFIG_HOME/mysync/conf.d/photos.toml" <<TOML
mode = "mirror"
left = "~/Pictures"
right = "sftp://me@server.example.org/srv/photos"
TOML
cat > "$XDG_CONFIG_HOME/mysync/conf.d/music.toml" <<TOML
mode = "update"
left = "~/Music"
right = "rclone:cloud:Music"
TOML
mysync sync -q documents
# Changes since that sync, on both sides.
mk "$L/Recipes/pancakes.md" "eggs, milk" "2026-09-12 09:00"
mk "$L/Notes/garden.txt" "tomatoes, basil, chili" "2026-09-20 18:00"
mk "$R/Projects/bike/parts.csv" "chain,tyres,brake pads" "2026-09-18 17:45"
mk "$R/Taxes/2025/letter.pdf" "%PDF letter" "2026-09-02 13:00"
rm "$L/Notes/reading-list.txt"
mv "$L/Projects/birdhouse" "$L/Projects/bird-feeder"
mk "$L/todo.txt" "call the plumber; buy paint" "2026-09-21 08:00"
mk "$R/todo.txt" "call the plumber, fix the gate" "2026-09-21 19:30"

# Selective sync: the archive holds every project, the laptop keeps a few.
A=/srv/archive/Projects
big() { mkdir -p "$(dirname "$1")"; head -c "$2" /dev/urandom > "$1"; touch -d "$3" "$1"; }
big "$A/2022/kitchen-remodel/photos.tar" 3400000 "2022-05-10 12:00"
big "$A/2022/kitchen-remodel/drawings.pdf" 820000 "2022-04-02 12:00"
big "$A/2023/greenhouse/plans.pdf" 1250000 "2023-03-18 12:00"
big "$A/2023/greenhouse/build-log.mp4" 5800000 "2023-06-30 12:00"
big "$A/2023/website/site.tar.gz" 640000 "2023-09-01 12:00"
big "$A/2024/canoe/lines.pdf" 410000 "2024-02-11 12:00"
big "$A/2024/canoe/build-photos.tar" 4600000 "2024-07-21 12:00"
big "$A/2025/boat-trailer/parts.ods" 90000 "2025-04-02 12:00"
big "$A/2025/boat-trailer/welding.mp4" 3900000 "2025-05-15 12:00"
big "$A/2026/bike/parts.csv" 2000 "2026-07-03 12:00"
big "$A/2026/birdhouse/plan.svg" 15000 "2026-06-14 16:05"
cat > "$XDG_CONFIG_HOME/mysync/conf.d/projects.toml" <<TOML
selective = "left"        # the laptop keeps only the selection; the archive holds everything
left = "~/Projects"
right = "/srv/archive/Projects"
TOML
mkdir -p "$HOME/Projects"
mysync select projects --drop "" --keep 2025 2026 >/dev/null
mysync sync -q projects
# Next round: bring the greenhouse back, free the boat trailer.
mysync select projects --keep 2023/greenhouse --drop 2025/boat-trailer >/dev/null
echo "demo home ready"
