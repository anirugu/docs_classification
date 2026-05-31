import os
import subprocess
from pathlib import Path

# Hardcoded paths
TO_SCAN_DIR = r"C:\Users\Anirudha\Downloads\books\to_scan"
CLASSIFY_SCRIPT = Path(__file__).parent / "classify_move_non_english.py"

def main():
    base_dir = Path(TO_SCAN_DIR)
    
    if not base_dir.exists() or not base_dir.is_dir():
        print(f"Error: The directory '{base_dir}' does not exist or is not a folder.")
        return

    # Find all direct subfolders (1 level deep)
    subfolders = [f for f in base_dir.iterdir() if f.is_dir()]
    
    if not subfolders:
        print(f"No subfolders found in {base_dir}")
        return

    print(f"Found {len(subfolders)} folders to scan in '{base_dir}'.\n")

    for index, folder in enumerate(sorted(subfolders), start=1):
        print(f"[{index}/{len(subfolders)}] ==================================================")
        print(f"Working on folder: {folder.name}")
        print(f"Full path: {folder}")
        print(f"===========================================================")
        
        # Call the existing classify script
        # subprocess.run will automatically stream the output to the console so you can watch it live
        try:
            subprocess.run(
                ["python", str(CLASSIFY_SCRIPT), str(folder)],
                check=True
            )
        except subprocess.CalledProcessError:
            print(f"\n[!] An error occurred while processing '{folder.name}'. Moving to the next folder...")
        except KeyboardInterrupt:
            print("\n[!] Batch process interrupted by user. Stopping.")
            break
            
        print("\n")

if __name__ == "__main__":
    main()
