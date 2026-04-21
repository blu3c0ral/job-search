"use client";

import { useState, useCallback } from "react";
import Link from "next/link";
import { Job, MatchDetail, TailoringChange, TailoringData } from "@/lib/types";

const MATCH_COLORS: Record<string, string> = {
  "Excelent Match": "bg-green-100 text-green-800 border-green-300",
  "Good Match": "bg-emerald-100 text-emerald-800 border-emerald-300",
  Borderline: "bg-yellow-100 text-yellow-800 border-yellow-300",
  Skip: "bg-orange-100 text-orange-800 border-orange-300",
  "Hard No": "bg-red-100 text-red-800 border-red-300",
  "Not Relevant": "bg-gray-100 text-gray-600 border-gray-300",
};

const STATUS_OPTIONS = [
  "New",
  "Applied",
  "Interviewing",
  "Rejected",
  "Offer",
  "Skipped",
];

export function JobDetail({ job }: { job: Job }) {
  const [status, setStatus] = useState(job.status);
  const [saving, setSaving] = useState<string | null>(null);
  const [tailorState, setTailorState] = useState<"idle" | "triggering" | "triggered" | "error">("idle");

  const save = useCallback(
    async (field: string, value: string) => {
      setSaving(field);
      try {
        await fetch("/api/update-job", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            id: job.id,
            source_platform: job.source_platform,
            [field]: value,
          }),
        });
      } finally {
        setSaving(null);
      }
    },
    [job.id, job.source_platform]
  );

  const matchDetail = job.match_detail as MatchDetail | null;
  // Normalize both data shapes: flat array or {changes, gaps} object
  const rawTailoring = job.tailoring_changes;
  let tailoringChanges: TailoringChange[] = [];
  let tailoringGaps: string[] = [];
  if (rawTailoring) {
    if (Array.isArray(rawTailoring)) {
      tailoringChanges = rawTailoring;
    } else {
      const data = rawTailoring as TailoringData;
      tailoringChanges = data.changes ?? [];
      tailoringGaps = data.gaps ?? [];
    }
  }

  return (
    <main className="flex flex-col flex-1 px-4 py-6 max-w-4xl mx-auto w-full">
      {/* Back link */}
      <Link href="/" className="text-accent hover:underline text-sm mb-4">
        &larr; Back to jobs
      </Link>

      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold">{job.role_title}</h1>
          <p className="text-lg text-gray-600">{job.company}</p>
          <p className="text-sm text-gray-400">
            {job.location} &middot; {job.date_found} &middot;{" "}
            {job.source_platform}
          </p>
          {job.compensation && job.compensation !== "Not listed" && (
            <p className="text-sm text-gray-500 mt-1">{job.compensation}</p>
          )}
        </div>
        <div className="flex items-center gap-3">
          {job.match && (
            <span
              className={`px-3 py-1 rounded-full text-sm font-medium border ${MATCH_COLORS[job.match] ?? "bg-gray-100"}`}
            >
              {job.match}
              {matchDetail?.score != null && ` (${matchDetail.score})`}
            </span>
          )}
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              save("status", e.target.value);
            }}
            className="border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
            {/* Include current status if not in the predefined list */}
            {!STATUS_OPTIONS.includes(status) && (
              <option value={status}>{status}</option>
            )}
          </select>
        </div>
      </div>

      {/* Links bar */}
      <div className="flex flex-wrap gap-3 mb-8">
        {job.link && (
          <a
            href={job.link}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-accent text-white rounded-md text-sm font-medium hover:bg-blue-700 transition-colors"
          >
            View Job Description &rarr;
          </a>
        )}
        <a
          href={`https://www.google.com/search?q=${encodeURIComponent(job.company + " " + job.role_title)}`}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 px-4 py-2 border border-gray-400 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-100 transition-colors"
        >
          Search on Google
        </a>
        {job.apply_url && job.apply_url !== job.link && (
          <a
            href={job.apply_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 px-4 py-2 border border-accent text-accent rounded-md text-sm font-medium hover:bg-blue-50 transition-colors"
          >
            Apply &rarr;
          </a>
        )}
        {job.tailored_resume && (
          <a
            href={`/api/download-resume?path=${encodeURIComponent(job.tailored_resume)}`}
            className="inline-flex items-center gap-1.5 px-4 py-2 bg-green-600 text-white rounded-md text-sm font-medium hover:bg-green-700 transition-colors"
          >
            Download Resume (.docx)
          </a>
        )}
        <button
            onClick={async () => {
              setTailorState("triggering");
              try {
                const res = await fetch("/api/trigger-tailor", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    platform: job.source_platform,
                    job_id: job.id,
                  }),
                });
                if (res.ok) {
                  setTailorState("triggered");
                } else {
                  const data = await res.json();
                  console.error("Tailor trigger failed:", data.error);
                  setTailorState("error");
                }
              } catch {
                setTailorState("error");
              }
            }}
            disabled={tailorState === "triggering" || tailorState === "triggered"}
            className={`inline-flex items-center gap-1.5 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              tailorState === "triggered"
                ? "bg-green-100 text-green-800 border border-green-300"
                : tailorState === "error"
                  ? "bg-red-100 text-red-800 border border-red-300 hover:bg-red-200"
                  : "bg-purple-600 text-white hover:bg-purple-700"
            } disabled:opacity-60`}
          >
            {tailorState === "idle" && "Tailor Resume"}
            {tailorState === "triggering" && "Triggering..."}
            {tailorState === "triggered" && "Triggered! Check GitHub Actions"}
            {tailorState === "error" && "Failed — Retry?"}
          </button>
      </div>

      {/* Narrative fields */}
      <section className="mb-8">
        <h2 className="text-lg font-semibold mb-3">Application Narratives</h2>
        <div className="flex flex-col gap-4">
          <NarrativeField
            label="Why this company?"
            field="why_this_company"
            value={job.why_this_company}
            saving={saving}
            onSave={save}
          />
          <NarrativeField
            label="Why this role?"
            field="why_this_role"
            value={job.why_this_role}
            saving={saving}
            onSave={save}
          />
          <NarrativeField
            label="Something I built and I'm proud of"
            field="something_i_built_and_proud_of"
            value={job.something_i_built_and_proud_of}
            saving={saving}
            onSave={save}
          />
        </div>
      </section>

      {/* Match detail */}
      {matchDetail && (
        <Collapsible title="Match Details" defaultOpen>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
            <DetailCard label="Bottom Line" value={matchDetail.bottom_line} />
            <DetailCard
              label="Where It Aligns"
              value={matchDetail.where_it_aligns}
            />
            <DetailCard
              label="Where It Breaks Down"
              value={matchDetail.where_it_breaks_down}
            />
            <DetailCard
              label="Pre-screen"
              value={
                matchDetail.pre_screen
                  ? `${matchDetail.pre_screen_category}: ${matchDetail.pre_screen}`
                  : null
              }
            />
            <DetailCard label="Comp Risk" value={matchDetail.comp_risk} />
            <DetailCard label="Comp Note" value={matchDetail.comp_note} />
            {matchDetail.dealbreaker_triggered && (
              <div className="col-span-full bg-red-50 border border-red-200 rounded-md px-3 py-2 text-red-700 font-medium">
                Dealbreaker triggered
              </div>
            )}
          </div>
        </Collapsible>
      )}

      {/* Tailoring details */}
      {(tailoringChanges.length > 0 || tailoringGaps.length > 0) && (
        <Collapsible title="Resume Tailoring">
          <div className="flex flex-col gap-4">
            {tailoringChanges.map((change, i) => (
              <div key={i} className="border border-border rounded-md p-3 text-sm">
                {change.why && (
                  <p className="text-xs font-medium text-accent mb-2">{change.why}</p>
                )}
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <p className="text-xs font-medium text-gray-500 mb-1">Original</p>
                    <p className="text-gray-600">{change.original}</p>
                  </div>
                  <div>
                    <p className="text-xs font-medium text-gray-500 mb-1">Tailored</p>
                    <p className="text-gray-800">{change.tailored}</p>
                  </div>
                </div>
              </div>
            ))}
            {tailoringGaps.length > 0 && (
              <div>
                <h4 className="font-medium text-sm mb-1">Gaps identified:</h4>
                <ul className="list-disc list-inside text-sm text-gray-700 space-y-0.5">
                  {tailoringGaps.map((g, i) => (
                    <li key={i}>{g}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </Collapsible>
      )}

      {/* Job description */}
      {job.job_description && (
        <Collapsible title="Full Job Description">
          <pre className="whitespace-pre-wrap text-sm text-gray-700 font-sans leading-relaxed max-h-[600px] overflow-auto">
            {job.job_description}
          </pre>
        </Collapsible>
      )}
    </main>
  );
}

/* --- Sub-components --- */

function NarrativeField({
  label,
  field,
  value,
  saving,
  onSave,
}: {
  label: string;
  field: string;
  value: string | null;
  saving: string | null;
  onSave: (field: string, value: string) => Promise<void>;
}) {
  const [text, setText] = useState(value ?? "");
  const [copied, setCopied] = useState(false);
  const [dirty, setDirty] = useState(false);

  function handleCopy() {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  function handleBlur() {
    if (dirty) {
      onSave(field, text);
      setDirty(false);
    }
  }

  const isSaving = saving === field;

  return (
    <div className="border border-border rounded-lg p-3">
      <div className="flex items-center justify-between mb-1.5">
        <label className="text-sm font-medium text-gray-700">{label}</label>
        <div className="flex items-center gap-2">
          {isSaving && (
            <span className="text-xs text-gray-400">Saving...</span>
          )}
          {!isSaving && dirty && (
            <span className="text-xs text-amber-500">Unsaved</span>
          )}
          {!isSaving && !dirty && text && (
            <span className="text-xs text-green-500">Saved</span>
          )}
          <button
            onClick={handleCopy}
            disabled={!text}
            className="text-xs px-2 py-0.5 rounded border border-border hover:bg-muted transition-colors disabled:opacity-30"
          >
            {copied ? "Copied!" : "Copy"}
          </button>
        </div>
      </div>
      <textarea
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setDirty(true);
        }}
        onBlur={handleBlur}
        rows={3}
        className="w-full text-sm border border-border rounded-md px-2 py-1.5 resize-y focus:outline-none focus:ring-2 focus:ring-accent"
        placeholder="Not yet generated..."
      />
    </div>
  );
}

function Collapsible({
  title,
  children,
  defaultOpen = false,
}: {
  title: string;
  children: React.ReactNode;
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className="mb-4 border border-border rounded-lg">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-3 text-left font-medium text-sm hover:bg-muted transition-colors rounded-lg"
      >
        {title}
        <span className="text-gray-400">{open ? "\u25B2" : "\u25BC"}</span>
      </button>
      {open && <div className="px-4 pb-4">{children}</div>}
    </section>
  );
}

function DetailCard({
  label,
  value,
}: {
  label: string;
  value: string | null | undefined;
}) {
  if (!value) return null;
  return (
    <div className="bg-muted rounded-md px-3 py-2">
      <p className="text-xs font-medium text-gray-500 mb-0.5">{label}</p>
      <p className="text-gray-800">{value}</p>
    </div>
  );
}
