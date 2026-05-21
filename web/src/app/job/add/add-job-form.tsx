"use client";

import { useState } from "react";

const STATUS_OPTIONS = [
  "New",
  "Applied",
  "Interviewing",
  "Rejected",
  "Offer",
  "Skipped",
];

const APPLIED_STATUSES = new Set(["Applied", "Interviewing", "Rejected", "Offer"]);

function today() {
  return new Date().toISOString().split("T")[0];
}

export function AddJobForm() {
  const [company, setCompany] = useState("");
  const [roleTitle, setRoleTitle] = useState("");
  const [jobDescription, setJobDescription] = useState("");
  const [location, setLocation] = useState("");
  const [compensation, setCompensation] = useState("");
  const [link, setLink] = useState("");
  const [applyUrl, setApplyUrl] = useState("");
  const [status, setStatus] = useState("New");
  const [dateFound, setDateFound] = useState(today());
  const [appliedDate, setAppliedDate] = useState(today());

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const showAppliedDate = APPLIED_STATUSES.has(status);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!company.trim() || !roleTitle.trim() || !jobDescription.trim()) {
      setError("Company, role title, and job description are required.");
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      const body: Record<string, string> = {
        company: company.trim(),
        role_title: roleTitle.trim(),
        job_description: jobDescription.trim(),
        status,
        date_found: dateFound,
      };
      if (location.trim()) body.location = location.trim();
      if (compensation.trim()) body.compensation = compensation.trim();
      if (link.trim()) body.link = link.trim();
      if (applyUrl.trim()) body.apply_url = applyUrl.trim();
      if (showAppliedDate && appliedDate) body.applied_date = appliedDate;

      const res = await fetch("/api/create-job", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? "Failed to create job.");
        setSubmitting(false);
        return;
      }

      window.location.href = `/job/${encodeURIComponent(data.platform)}/${encodeURIComponent(data.id)}`;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unexpected error.");
      setSubmitting(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-5">
      {error && (
        <div className="bg-red-50 border border-red-200 text-red-700 rounded-md px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {/* Core identity */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Field label="Company" required>
          <input
            type="text"
            value={company}
            onChange={(e) => setCompany(e.target.value)}
            placeholder="e.g. Anthropic"
            className={inputClass}
            required
          />
        </Field>
        <Field label="Role Title" required>
          <input
            type="text"
            value={roleTitle}
            onChange={(e) => setRoleTitle(e.target.value)}
            placeholder="e.g. Senior Backend Engineer"
            className={inputClass}
            required
          />
        </Field>
      </div>

      {/* Job description — largest field */}
      <Field label="Job Description" required>
        <textarea
          value={jobDescription}
          onChange={(e) => setJobDescription(e.target.value)}
          placeholder="Paste the full job description here..."
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
            placeholder="e.g. New York, NY (Hybrid)"
            className={inputClass}
          />
        </Field>
        <Field label="Compensation">
          <input
            type="text"
            value={compensation}
            onChange={(e) => setCompensation(e.target.value)}
            placeholder="e.g. $180K–$240K + equity"
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
            placeholder="https://..."
            className={inputClass}
          />
        </Field>
        <Field label="Apply URL" hint="Leave blank to use posting URL">
          <input
            type="url"
            value={applyUrl}
            onChange={(e) => setApplyUrl(e.target.value)}
            placeholder="https://..."
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

      {/* Actions */}
      <div className="flex items-center gap-3 pt-2">
        <button
          type="submit"
          disabled={submitting}
          className="px-6 py-2.5 bg-accent text-white rounded-md text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-60"
        >
          {submitting ? "Evaluating & saving..." : "Add Job"}
        </button>
        <a
          href="/"
          className="px-4 py-2.5 text-sm text-gray-600 hover:text-gray-900 transition-colors"
        >
          Cancel
        </a>
        {submitting && (
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
        {hint && <span className="text-gray-400 font-normal ml-1 text-xs">({hint})</span>}
      </label>
      {children}
    </div>
  );
}
