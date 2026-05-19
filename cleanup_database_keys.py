import os
import requests
import tempfile
import shutil
import glob
import traceback
from dotenv import load_dotenv
from music21 import converter, key, pitch

load_dotenv()

# Import your newly updated functions from app.py
from app import (
    analyse_musicxml_summary, 
    deduplicate_by_key,
    make_json_safe, 
    supabase, 
    extract_vocal_note_info,
    get_key_analysis_info,
    simplify_enharmonic,
    ensure_music21_key,
    get_note_comfort_category,
    get_note_comfort_color
)

def run_database_cleanup():
    if supabase is None:
        print("❌ Supabase client not initialized. Check your .env file.")
        return

    # Fetch all songs that have an MXL file URL available
    response = supabase.table('songs').select("*").not_.is_("mxl_url", "null").execute()
    songs = response.data

    print(f"Loaded {len(songs)} songs from Supabase for structure cleanup.")

    for song in songs:
        pdf_hash = song['pdf_hash']
        mxl_url = song['mxl_url']
        title = song.get('title', 'Unknown Title')
        
        print(f"\n🧼 Re-analyzing Structure: {title}...")
        temp_dir = tempfile.mkdtemp()
        
        try:
            # 1. Download the existing MXL file from storage
            mxl_path = os.path.join(temp_dir, f"{pdf_hash}.mxl")
            r = requests.get(mxl_url)
            r.raise_for_status()
            with open(mxl_path, 'wb') as f:
                f.write(r.content)

            # 2. Re-run through your fixed app logic to get a 12-key array
            summary = None
            try:
                summary = analyse_musicxml_summary(
                    output_dir=temp_dir,
                    name=pdf_hash,
                    prefer_transpose_keys=False,
                    pdf_metadata={"ccli_number": song.get("ccli_number")}
                )
            except Exception as e:
                print(f"   ℹ️ app.py native summary analysis exception: {e}")

            # 3. Validation / Recovery Block if native app loop fails
            if not summary or not summary.get("all_keys_analysis") or summary.get("original_key_info", {}).get("name") == "Unknown":
                print(f"   🛠️ Activating Manual Extraction Recovery for: {title}")
                
                patched_files = glob.glob(os.path.join(tempfile.gettempdir(), f"{pdf_hash}.mxl_patched.xml"))
                parse_target = patched_files[0] if patched_files else mxl_path
                
                score = converter.parse(parse_target)
                notes, _, _ = extract_vocal_note_info(score, source_name=title)
                
                if not notes:
                    print(f"   ⚠️ Skipping {title}: No vocal notes could be parsed.")
                    continue

                ks_objects = score.parts[0].recurse().getElementsByClass(key.KeySignature)
                detected_key = ks_objects[0].asKey() if ks_objects else score.analyze('key')
                detected_key = simplify_enharmonic(ensure_music21_key(detected_key))
                
                # Re-calculate transposition array
                all_keys_analysis = get_key_analysis_info(detected_key, notes, prefer_transpose_keys=False)
                
                # Safeguard: if calculation fails completely, create a safe dummy container or skip
                if not all_keys_analysis:
                    print(f"   ⚠️ Skipping {title}: key analysis generation returned nothing.")
                    continue

                # --- FORCED PITCH CLASS DEDUPLICATION ON FALLBACK ---
                deduped_keys = deduplicate_by_key(all_keys_analysis)
                recommended = deduped_keys[0] if deduped_keys else None
                
                if recommended and 'low' in recommended:
                    recommended['low_comfort'] = get_note_comfort_category(recommended['low'])
                    recommended['high_comfort'] = get_note_comfort_category(recommended['high'])
                    recommended['low_color'] = get_note_comfort_color(recommended['low'])
                    recommended['high_color'] = get_note_comfort_color(recommended['high'])

                other_keys = [k for k in deduped_keys if recommended and k.get('shift') != recommended.get('shift')]
                original_key_info_item = next((k for k in all_keys_analysis if k.get('shift') == 0), None)

                midi_values = [n['midi'] for n in notes]
                low_midi = min(midi_values) if midi_values else 60
                high_midi = max(midi_values) if midi_values else 72
                
                summary = {
                    "title": title,
                    "ccli_number": song.get("ccli_number", "Not found"),
                    "recommended": recommended,
                    "other_keys": other_keys,           # Exactly 11 keys
                    "original_key_info": {
                        "name": str(detected_key if detected_key else "Unknown").replace('-', '♭'),
                        "range_low": pitch.Pitch(low_midi).nameWithOctave,
                        "range_high": pitch.Pitch(high_midi).nameWithOctave,
                        "low_comfort": original_key_info_item["low_comfort"] if original_key_info_item else "ideal",
                        "high_comfort": original_key_info_item["high_comfort"] if original_key_info_item else "ideal",
                        "low_color": original_key_info_item["low_color"] if original_key_info_item else "green",
                        "high_color": original_key_info_item["high_color"] if original_key_info_item else "green",
                        "comfort_score": original_key_info_item["comfort_score"] if original_key_info_item else 0.0,
                        "comfort_label": original_key_info_item["comfort_label"] if original_key_info_item else "⚠️ Unknown"
                    },
                    "skipped": [],
                    "warnings": []
                }

            # 4. Push updated data structure back to Supabase
            db_summary = make_json_safe(summary)
            supabase.table('songs').update({
                "analysis_results": db_summary,
                "original_key": summary["original_key_info"]["name"],
                "lowest_note": summary["original_key_info"]["range_low"],
                "highest_note": summary["original_key_info"]["range_high"]
            }).eq("pdf_hash", pdf_hash).execute()

            print(f"✅ Cleaned and Updated: {title} ({summary['original_key_info']['name']}) -> 12 Keys Matrix Saved.")

        except Exception as e:
            print(f"❌ Failed processing for {title}: {e}")
            traceback.print_exc()
        finally:
            shutil.rmtree(temp_dir)

if __name__ == "__main__":
    run_database_cleanup()