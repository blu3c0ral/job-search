"use client";

import { useState } from "react";
import { Job } from "@/lib/types";

const STATUS_OPTIONS = [
  "New",
  "Applied",
  "Interviewing",
  "Rejected",
  "Offer",
  "Skipped",
];

const APPLIED_STATUSES = new Set(["Applied", "Interviewing", "Rejected", "Offer"]);

export function EditJobForm({ job }: { job: Job }) {
  const [company, setCompany] = useState(job.company ?? "");
  const [roleTitle, setRoleTitle] = useState(job.role_title ?? "");
  const [jobDescription, setJobDescription] = useState(job.job_description ?? "");
  const [location, setLocation] = useState(job.location ?? "");
  const [compensation, setCompensation] = useState(job.compensation ?? "");
  const [link, setLink] = useState(job.link ?? "");
  const [applyUrl, setApplyUrl] = useState(job.apply_url ?? "");
  const [status, setStatus] = useState(job.status ?? "New");
  const [dateFound, setDateFound] = useState(job.date_found ?? "");
  const [appliedDate, setAppliedDate] = useState(job.applied_date ?? "");

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadedPath, setUploadedPath] = useState<string | null>(job.tailored_resume ?? null);

  const showAppliedDate = APPLIED_STATUSES.has(status);
  const jdChanged = jobDescription.trim() !== (job.job_description ?? "").trim();

  const detailUrl = `/job/${encodeURIComponent(job.source_platform)}/${encodeURIComponent(job.id)}`;

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!company.trim() || !roleTitle.trim() || !jobDescription.trim()) {
      setError("Company, role title, and job description are required.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const body: Record<string, unknown> = {
        id: job.id,
        source_platform: job.source_platform,
        company: company.trim(),
        role_title: roleTitle.trim(),
        job_description: jobDescription.trim(),
        status,
        date_found: dateFound,
        rerun_match: jdChanged,
      };
      if (location.trim()) body.location = location.trim();
      if (compensation.trim()) body.compensation = compensation.trim();
      if (link.trim()) body.link = link.trim();
      if (applyUrl.trim()) body.apply_url = applyUrl.trim();
      if (showAppliedDate && appliedDate) body.applied_date = appliedDate;

      const res = await fetch("/api/update-job-details", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? "Failed to save changes.");
        setSubmitting(false);
        return;
      }

      window.location.href = detailUrl;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unexpected error.");
      setSubmitting(false);
    }
  }

  async function handleUpload() {
    if (!resumeFile) return;
    setUploading(true);
    setUploadError(null);
    try {
      const fd = new FormData();
      fd.append("file", resumeFile);
      fd.append("id", job.id);
      fd.append("source_platform", job.source_platform);
      const res = await fetch("/api/upload-resume", { method: "POST", body: fd });
      const data = await res.json();
      if (!res.ok) {
        setUploadError(data.error ?? "Upload failed.");
      } else {
        setUploadedPath(data.path);
        setResumeFile(null);
      }
    } catch (err) {
      setUploadError(err instanceof Error ? err.message : "Unexpected error.");
    } finally {
      setUploading(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-5">
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-md px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {jdChanged && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 rounded-md px-4 py-3 text-sm">
          Job description changed — match evaluation will re-run on save (~5s).
        </div>
      )}

      {/* Core identity */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Company" required>
          <input
            type="text"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
        <Field label="Role Title" required>
          <input
            type="text"
            value={roleTitle}
            onChange={(e) => setRoleTitle(e.target.value)}
            className={inputClass}
            required
          />
        </Field>
      </div>

      {/* Job description */}
      <Field label="Job Description" required>
        <textarea
          value={jobDescription}
          onChange={(e) => setJobDescription(e.target.value)}
          rows={14}
          className={`${inputClass} resize-y font-mono text-xs`}
          required
        />
      </Field>

      {/* Location & compensation */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Location">
          <input
            type="text"
            value={location}
            onChange={(e) => setLocation(e.target.value)}
            className={inputClass}
          />
        </Field>
        <Field label="Compensation">
          <input
            type="text"
            value={compensation}
            onChange={(e) => setCompensation(e.target.value)}
            className={inputClass}
          />
        </Field>
      </div>

      {/* URLs */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Job Posting URL">
          <input
            type="url"
            value={link}
            onChange={(e) => setLink(e.target.value)}
            className={inputClass}
          />
        </Field>
        <Field label="Apply URL" hint="Leave blank to use posting URL">
          <input
            type="url"
            value={applyUrl}
            onChange={(e) => setApplyUrl(e.target.value)}
            className={inputClass}
          />
        </Field>
      </div>

      {/* Status & dates */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <Field label="Status">
          <select
            value={status}
            onChange={(e) => setStatus(e.target.value)}
            className={inputClass}
          >
            {STATUS_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Date Found">
          <input
            type="date"
            value={dateFound}
            onChange={(e) => setDateFound(e.target.value)}
            className={inputClass}
          />
        </Field>
        {showAppliedDate && (
          <Field label="Applied Date">
            <input
              type="date"
              value={appliedDate}
              onChange={(e) => setAppliedDate(e.target.value)}
              className={inputClass}
            />
          </Field>
        )}
      </div>

      {/* Resume upload */}
      <div className="border border-border rounded-lg p-4 flex flex-col gap-3">
        <div className="flex items-center justify-between">
          <label className="text-sm font-medium text-gray-700">Resume (.docx)</label>
          {uploadedPath && (
            <a
              href={`/api/download-resume?path=${encodeURIComponent(uploadedPath)}`}
              className="text-xs text-accent hover:underline"
            >
              Download current
            </a>
          )}
        </div>
        {uploadedPath && !resumeFile && (
          <p className="text-xs text-green-700 bg-green-50 border border-green-200 rounded px-3 py-1.5">
            Resume on file: {uploadedPath.split("/").pop()}
          </p>
        )}
        <div className="flex items-center gap-3">
          <input
            type="file"
            accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            onChange={(e) => {
              setResumeFile(e.target.files?.[0] ?? null);
              setUploadError(null);
            }}
            className="text-sm text-gray-600 file:mr-3 file:py-1 file:px-3 file:rounded file:border file:border-border file:text-xs file:text-gray-700 file:bg-muted hover:file:bg-gray-200 file:transition-colors"
          />
          {resumeFile && (
            <button
              type="button"
              onClick={handleUpload}
              disabled={uploading}
              className="px-4 py-1.5 bg-green-600 text-white rounded-md text-sm font-medium hover:bg-green-700 transition-colors disabled:opacity-60"
            >
              {uploading ? "Uploading..." : "Upload"}
            </button>
          )}
        </div>
        {uploadError && (
          <p className="text-xs text-red-600">{uploadError}</p>
        )}
        {uploadedPath && resumeFile && (
          <p className="text-xs text-amber-700">Uploading will replace the existing resume.</p>
        )}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3 pt-2">
        <button
          type="submit"
          disabled={submitting}
          className="px-6 py-2.5 bg-accent text-white rounded-md text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-60"
        >
          {submitting
            ? jdChanged
              ? "Re-evaluating & saving..."
              : "Saving..."
            : "Save Changes"}
        </button>
        <a
          href={detailUrl}
          className="px-4 py-2.5 text-sm text-gray-600 hover:text-gray-900 transition-colors"
        >
          Cancel
        </a>
        {submitting && jdChanged && (
          <span className="text-xs text-gray-400">
            Running match evaluation — this takes a few seconds...
          </span>
        )}
      </div>
    </form>
  );
}

const inputClass =
  "w-full border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent bg-white";

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-gray-700">
        {label}
        {required && <span className="text-red-500 ml-0.5">*</span>}
        {hint && (
          <span className="text-gray-400 font-normal ml-1 text-xs">
            ({hint})
          </span>
        )}
      </label>
      {children}
    </div>
  );
}
