import os
from supabase import create_client, Client
from dotenv import load_dotenv

# LOAD THE .ENV FILE FIRST
load_dotenv() 

# NOW FETCH THE VARIABLES SAFELY
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# Check if they actually loaded
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("❌ Error: SUPABASE_URL or SUPABASE_SERVICE_KEY not found in .env file.")

# Initialize Supabase Client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


result = supabase.table("songs").update({"first_line": None}).eq("first_line", "Unknown").execute()
print("Database cleaned: All 'Unknown' first lines are now NULL.")