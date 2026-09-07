import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Markdown → HTML, matching Mynt's react-markdown + remark-gfm. Links open in a new tab.
export function Md({ children, className }: { children: string; className?: string }) {
  return (
    <div className={className}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node: _n, ...p }) => <a {...p} target="_blank" rel="noopener" />,
        }}
      >
        {children || ""}
      </ReactMarkdown>
    </div>
  );
}
