import { AddJobForm } from "./add-job-form";

export default function AddJobPage() {
  return (
    <main className="flex flex-col flex-1 px-4 py-6 max-w-3xl mx-auto w-full">
      <a href="/" className="text-accent hover:underline text-sm mb-4">
        &larr; Back to jobs
      </a>
      <h1 className="text-2xl font-bold mb-6">Add Job</h1>
      <AddJobForm />
    </main>
  );
}
