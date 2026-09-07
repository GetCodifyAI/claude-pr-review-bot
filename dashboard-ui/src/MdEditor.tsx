import { useState } from "react";
import { Md } from "./Md";

// A markdown field with Preview (formatted) / Edit (code) tabs — ports the old md_editor.
export function MdEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [edit, setEdit] = useState(false);
  return (
    <div className="mdwrap">
      <div className="ftabs">
        <button type="button" className={"fmode" + (!edit ? " on" : "")} onClick={() => setEdit(false)}>
          Preview
        </button>
        <button type="button" className={"fmode" + (edit ? " on" : "")} onClick={() => setEdit(true)}>
          Edit
        </button>
      </div>
      {edit ? (
        <textarea className="fedit" value={value} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <Md className="fpreview">{value || "_Nothing to preview._"}</Md>
      )}
    </div>
  );
}
