"use client";

import { useState, useRef, useEffect } from "react";

interface Message {
  role: "user" | "assistant";
  content: string;
}

const FIELD_OPTIONS = [
  { value: "why_this_company", label: "Why this company?" },
  { value: "why_this_role", label: "Why this role?" },
  {
    value: "something_i_built_and_proud_of",
    label: "Something I built and I'm proud of",
  },
];

const MODEL_OPTIONS = [
  { value: "sonnet", label: "Sonnet 4.6" },
  { value: "opus", label: "Opus 4.6" },
];

export function RephraseChat({
  jobId,
  sourcePlatform,
  resumeText,
}: {
  jobId: string;
  sourcePlatform: string;
  resumeText: string;
}) {
  const [open, setOpen] = useState(false);
  const [field, setField] = useState(FIELD_OPTIONS[0].value);
  const [model, setModel] = useState(MODEL_OPTIONS[0].value);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function send() {
    const text = input.trim();
    if (!text || loading) return;

    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: text }]);
    setLoading(true);

    try {
      const res = await fetch("/api/chat-rephrase", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          id: jobId,
          source_platform: sourcePlatform,
          field,
          userMessage: text,
          model,
          resumeText,
        }),
      });

      const data = await res.json();
      if (res.ok) {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: data.response },
        ]);
      } else {
        setMessages((prev) => [
          ...prev,
          { role: "assistant", content: `Error: ${data.error}` },
        ]);
      }
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: "Error: failed to reach API" },
      ]);
    } finally {
      setLoading(false);
    }
  }

  return (
    <>
      {/* Floating button */}
      <button
        onClick={() => setOpen(!open)}
        className="fixed bottom-6 right-6 w-14 h-14 bg-purple-600 text-white rounded-full shadow-lg hover:bg-purple-700 transition-colors flex items-center justify-center text-2xl z-50"
        title="Rephrase helper"
      >
        {open ? "\u00D7" : "\u270E"}
      </button>

      {/* Chat panel */}
      {open && (
        <div className="fixed bottom-24 right-6 w-96 max-h-[520px] bg-white border border-border rounded-xl shadow-2xl flex flex-col z-50">
          {/* Header */}
          <div className="px-4 py-3 border-b border-border">
            <h3 className="font-semibold text-sm mb-2">Rephrase Helper</h3>
            <div className="flex gap-2">
              <select
                value={field}
                onChange={(e) => setField(e.target.value)}
                className="flex-1 border border-border rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-accent"
              >
                {FIELD_OPTIONS.map((f) => (
                  <option key={f.value} value={f.value}>
                    {f.label}
                  </option>
                ))}
              </select>
              <select
                value={model}
                onChange={(e) => setModel(e.target.value)}
                className="border border-border rounded-md px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-accent"
              >
                {MODEL_OPTIONS.map((m) => (
                  <option key={m.value} value={m.value}>
                    {m.label}
                  </option>
                ))}
              </select>
            </div>
          </div>

          {/* Messages */}
          <div className="flex-1 overflow-auto px-4 py-3 space-y-3 min-h-[200px]">
            {messages.length === 0 && (
              <p className="text-xs text-gray-400 text-center mt-8">
                Select a field above, then ask for help rephrasing.
                <br />
                e.g. &ldquo;Make it more specific to their ML platform&rdquo;
              </p>
            )}
            {messages.map((msg, i) => (
              <div
                key={i}
                className={`text-sm ${
                  msg.role === "user" ? "text-right" : "text-left"
                }`}
              >
                <div
                  className={`inline-block max-w-[85%] px-3 py-2 rounded-lg ${
                    msg.role === "user"
                      ? "bg-accent text-white"
                      : "bg-gray-100 text-gray-800"
                  }`}
                >
                  <span className="whitespace-pre-wrap">{msg.content}</span>
                  {msg.role === "assistant" && (
                    <button
                      onClick={() => {
                        navigator.clipboard.writeText(msg.content);
                      }}
                      className="block mt-1 text-xs text-gray-400 hover:text-gray-600"
                    >
                      Copy
                    </button>
                  )}
                </div>
              </div>
            ))}
            {loading && (
              <div className="text-left">
                <div className="inline-block px-3 py-2 rounded-lg bg-gray-100 text-gray-400 text-sm">
                  Thinking...
                </div>
              </div>
            )}
            <div ref={bottomRef} />
          </div>

          {/* Input */}
          <div className="px-4 py-3 border-t border-border">
            <div className="flex gap-2">
              <input
                type="text"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
                placeholder="How should it be rephrased?"
                className="flex-1 border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent"
                disabled={loading}
              />
              <button
                onClick={send}
                disabled={loading || !input.trim()}
                className="px-3 py-1.5 bg-accent text-white rounded-md text-sm font-medium hover:bg-blue-700 transition-colors disabled:opacity-40"
              >
                Send
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
