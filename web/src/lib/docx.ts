import JSZip from "jszip";
import type { SupabaseClient } from "@supabase/supabase-js";

export async function extractDocxText(
  supabase: SupabaseClient,
  storagePath: string
): Promise<string> {
  const { data, error } = await supabase.storage
    .from("tailored-resumes")
    .download(storagePath);

  if (error || !data) return "";

  const zip = await JSZip.loadAsync(await data.arrayBuffer());
  const docXml = await zip.file("word/document.xml")?.async("string");
  if (!docXml) return "";

  return docXml
    .replace(/<w:br[^>]*\/>/g, "\n")
    .replace(/<w:tab[^>]*\/>/g, "\t")
    .replace(/<\/w:p>/g, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}
