import { NextRequest, NextResponse } from "next/server";
import { getSupabase } from "@/lib/supabase";

const BUCKET = "tailored-resumes";

export async function POST(request: NextRequest) {
  const formData = await request.formData();
  const file = formData.get("file") as File | null;
  const id = formData.get("id") as string | null;
  const source_platform = formData.get("source_platform") as string | null;

  if (!file || !id || !source_platform) {
    return NextResponse.json(
      { error: "file, id, and source_platform are required" },
      { status: 400 }
    );
  }

  const storagePath = `${source_platform}/${id}/resume.docx`;
  const bytes = await file.arrayBuffer();

  const supabase = getSupabase();

  const { error: uploadError } = await supabase.storage
    .from(BUCKET)
    .upload(storagePath, bytes, {
      contentType:
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
      upsert: true,
    });

  if (uploadError) {
    return NextResponse.json({ error: uploadError.message }, { status: 500 });
  }

  const { error: dbError } = await supabase
    .from("job_search_main")
    .update({ tailored_resume: storagePath })
    .eq("id", id)
    .eq("source_platform", source_platform);

  if (dbError) {
    return NextResponse.json({ error: dbError.message }, { status: 500 });
  }

  return NextResponse.json({ ok: true, path: storagePath });
}
