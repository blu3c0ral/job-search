import os
import re
from supabase import create_client

from dotenv import load_dotenv

load_dotenv()

# 1. Setup
url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")  # Use service_role for migration
supabase = create_client(url, key)

md_folder_path = os.getenv("MD_FOLDER_PATH")  # Path to your unzipped folder


def parse_jd_file(filepath):
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Regex to find metadata and capture everything after 'Status: New'
    # It looks for 'Source Platform' and 'ID' specifically
    source_match = re.search(r"Source Platform:\s*(.*)", content)
    id_match = re.search(r"ID:\s*(.*)", content)

    # The JD starts after the Status line
    # We split at the Status line and take the second part
    parts = re.split(r"Status:.*\n", content)
    jd_body = parts[1].strip() if len(parts) > 1 else ""

    return {
        "source": source_match.group(1).strip() if source_match else None,
        "id": id_match.group(1).strip() if id_match else None,
        "content": jd_body,
    }


# 2. Iterate and Update
for filename in os.listdir(md_folder_path):
    if filename.endswith(".md"):
        file_data = parse_jd_file(os.path.join(md_folder_path, filename))

        if file_data["source"] and file_data["id"]:
            print(f"Updating {file_data['source']} ID: {file_data['id']}...")

            # 3. Match by both Source and ID to ensure accuracy
            result = (
                supabase.table("job_search_main")
                .update({"job_description": file_data["content"]})
                .eq("source_platform", file_data["source"])
                .eq("id", file_data["id"])
                .execute()
            )

print("Migration complete.")
