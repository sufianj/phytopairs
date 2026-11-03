#!/usr/bin/env bash
# Copy all photos from <crop_name>/images/*photo*.* into a flat folder,
# renaming each as "<crop_name>#<original_filename>".

set -euo pipefail

# -------- configuration --------
SRC_DIR="${1:-.}"        # top folder containing the crop subfolders (default: current dir)
DEST_DIR="${2:-./photos_flat}"   # where the renamed photos will be collected
# -------------------------------

mkdir -p "$DEST_DIR"

# Loop over each crop subfolder
for crop_path in "$SRC_DIR"/*/; do
    [ -d "$crop_path" ] || continue
    crop_name="$(basename "$crop_path")"

    # Skip the destination folder if it happens to live inside SRC_DIR
    if [ "$(cd "$crop_path" && pwd)" = "$(cd "$DEST_DIR" && pwd)" ]; then
        continue
    fi

    images_dir="${crop_path}images"
    [ -d "$images_dir" ] || continue

    # Copy every file matching *photo*.* (non-recursive, one level)
    shopt -s nullglob
    for photo in "$images_dir"/*photo*.*; do
        [ -f "$photo" ] || continue
        filename="$(basename "$photo")"
        cp -- "$photo" "$DEST_DIR/${crop_name}#${filename}"
    done
    shopt -u nullglob
done

echo "Done. Photos copied to: $DEST_DIR"