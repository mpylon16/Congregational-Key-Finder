import pdfplumber
import sys

def debug_pdf_fonts(pdf_path):
    print(f"--- Diagnosing: {pdf_path} ---")
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            print("Error: PDF has no pages.")
            return
            
        first_page = pdf.pages[0]
        width = first_page.width
        height = first_page.height
        
        # 1. Test standard text extraction
        print("\n--- Standard Extract Text (First 500 chars) ---")
        raw_text = first_page.extract_text() or ""
        print(raw_text[:500])
        print("-----------------------------------------------")
        
        # 2. Inspect raw word objects at the top of the page
        print("\n--- Inspecting Top 20% Words (with sizes & bounds) ---")
        words = first_page.extract_words(extra_attrs=["size"])
        
        top_words = [w for w in words if w["bottom"] <= height * 0.25]
        # Sort left-to-right, top-to-bottom
        top_words.sort(key=lambda w: (w["top"], w["x0"]))
        
        for w in top_words[:30]: # Look at the first 30 structural elements
            word_text = w["text"]
            # Print the character representations and their raw Unicode numbers
            char_codes = [f"U+{ord(c):04X}" for c in word_text]
            print(f"Text: '{word_text}' | Codes: {char_codes} | Size: {w['size']:.1f} | Bounds: (x0: {w['x0']:.1f}, top: {w['top']:.1f})")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        debug_pdf_fonts(sys.argv[1])
    else:
        print("Please provide a path to a PDF file. Usage: python test_pdf.py your_sheet.pdf")