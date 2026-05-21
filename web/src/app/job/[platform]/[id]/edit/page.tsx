import { getSupabase } from "@/lib/supabase";
import { Job } from "@/lib/types";
import { EditJobForm } from "./edit-job-form";
import Link from "next/link";

export const dynamic = "force-dynamic";

export default async function EditJobPage({
  params,
}: {
  params: Promise<{ platform: string; id: string }>;
}) {
  const { platform, id } = await params;
  const decodedPlatform = decodeURIComponent(platform);
  const decodedId = decodeURIComponent(id);

  const supabase = getSupabase();
  const { data, error } = await supabase
    .from("job_search_main")
    .select("*")
    .eq("source_platform", decodedPlatform)
    .eq("id", decodedId)
    .single();

  if (error || !data) {
    return (
      <div className="p-8">
        <Link href="/" className="text-accent hover:underline">
          &larr; Back
        </Link>
        <p className="mt-4 text-red-600">
          Job not found: {error?.message ?? "No data"}
        </p>
      </div>
    );
  }

  const job = data as Job;
  const detailUrl = `/job/${encodeURIComponent(decodedPlatform)}/${encodeURIComponent(decodedId)}`;

  return (
    <main className="flex flex-col flex-1 px-4 py-6 max-w-3xl mx-auto w-full">
      <a href={detailUrl} className="text-accent hover:underline text-sm mb-4">
        &larr; Back to job
      </a>
      <h1 className="text-2xl font-bold mb-1">Edit Job</h1>
      <p className="text-gray-500 text-sm mb-6">
        {job.role_title} &middot; {job.company}
      </p>
      <EditJobForm job={job} />
    </main>
  );
}
