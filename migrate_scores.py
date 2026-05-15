import os
import requests
import tempfile
import shutil
import glob
from dotenv import load_dotenv
from music21 import converter, key, pitch

load_dotenv()
from app import (
    analyse_musicxml_summary, 
    make_json_safe, 
    supabase, 
    extract_vocal_note_info,
    calculate_comfort_score,
    get_key_analysis_info,
    comfort_category,
    comfort_category_slug,
    simplify_enharmonic,
    ensure_music21_key
)

def run_headless_migration():
    if supabase is None:
        print("❌ Supabase client not initialized.")
        return

    # Fetch all songs that have an MXL file URL
    response = supabase.table('songs').select("*").not_.is_("mxl_url", "null").execute()
    songs = response.data

    print(f"Loaded {len(songs)} songs for migration.")

    for song in songs:
        pdf_hash = song['pdf_hash']
        mxl_url = song['mxl_url']
        title = song.get('title', 'Unknown')
        
        print(f"\n🔄 Processing: {title}...")
        temp_dir = tempfile.mkdtemp()
        
        try:
            # 1. Download file
            mxl_path = os.path.join(temp_dir, f"{pdf_hash}.mxl")
            r = requests.get(mxl_url)
            r.raise_for_status()
            with open(mxl_path, 'wb') as f:
                f.write(r.content)

            # 2. Try the app analysis, but expect the layout crash on hashes
            summary = None
            try:
                summary = analyse_musicxml_summary(
                    output_dir=temp_dir,
                    name=pdf_hash,
                    prefer_transpose_keys=False,
                    pdf_metadata={"ccli_number": song.get("ccli_number")}
                )
            except Exception as e:
                print(f"   ℹ️ app.py native summary analysis threw an exception: {e}")

            # 3. Validation / Recovery Block
            if not summary or not summary.get("all_keys_analysis") or summary.get("original_key_info", {}).get("name") == "Unknown":
                print(f"   🛠️ Activating Manual Extraction Recovery for: {title}")
                
                # Use your app's pre-built patched file or fall back to native mxl
                patched_files = glob.glob(os.path.join(tempfile.gettempdir(), f"{pdf_hash}.mxl_patched.xml"))
                parse_target = patched_files[0] if patched_files else mxl_path
                
                score = converter.parse(parse_target)
                notes, _, _ = extract_vocal_note_info(score, source_name=title)
                
                if not notes:
                    print(f"   ⚠️ Skipping {title}: No vocal notes could be parsed.")
                    continue

                # Fallback Key Extraction
                ks_objects = score.parts[0].recurse().getElementsByClass(key.KeySignature)
                detected_key = ks_objects[0].asKey() if ks_objects else score.analyze('key')
                detected_key = simplify_enharmonic(ensure_music21_key(detected_key))
                
                # Re-calculate the transposition matrix safely
                all_keys_analysis = get_key_analysis_info(detected_key, notes, prefer_transpose_keys=False)
                original_key_info = next((k for k in all_keys_analysis if k['shift'] == 0), None)

                midi_values = [n['midi'] for n in notes]
                low_midi = min(midi_values)
                high_midi = max(midi_values)
                low_note_str = pitch.Pitch(low_midi).nameWithOctave
                high_note_str = pitch.Pitch(high_midi).nameWithOctave

                comfort_score = original_key_info["comfort_score"] if original_key_info else calculate_comfort_score(notes)

                # Reconstruct full schema
                summary = {
                    "title": title,
                    "ccli_number": song.get("ccli_number", "Not found"),
                    "all_keys_analysis": all_keys_analysis,
                    "recommended": all_keys_analysis[0] if all_keys_analysis else None,
                    "other_keys": [k for k in all_keys_analysis if k['shift'] != 0],
                    "original_key_info": {
                        "name": f"{detected_key.tonic.name} {detected_key.mode}",
                        "range_low": low_note_str,
                        "range_high": high_note_str,
                        "low_comfort": original_key_info["low_comfort"] if original_key_info else "ideal",
                        "high_comfort": original_key_info["high_comfort"] if original_key_info else "ideal",
                        "low_color": original_key_info["low_color"] if original_key_info else "green",
                        "high_color": original_key_info["high_color"] if original_key_info else "green",
                        "comfort_score": comfort_score,
                        "comfort_label": comfort_category(comfort_score)
                    },
                    "skipped": [],
                    "warnings": []
                }

            # 4. Save and Upload to Supabase Tables
            db_summary = make_json_safe(summary)
            supabase.table('songs').update({
                "analysis_results": db_summary,
                "original_key": summary["original_key_info"]["name"],
                "lowest_note": summary["original_key_info"]["range_low"],
                "highest_note": summary["original_key_info"]["range_high"]
            }).eq("pdf_hash", pdf_hash).execute()

            print(f"✅ Successfully Migrated: {title} ({summary['original_key_info']['name']})")

        except Exception as e:
            print(f"❌ Permanent Failure for {title}: {e}")
        finally:
            shutil.rmtree(temp_dir)

if __name__ == "__main__":
    run_headless_migration()