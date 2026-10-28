export type FieldType = "text" | "number" | "select";

export interface Field {
  name: string;
  type: FieldType;
  label: string;
  options?: string[];
  required?: boolean;
}

export interface FormErrors {
  [field: string]: string;
}

export function validate(fields: Field[], values: Record<string, string>): FormErrors {
  const errors: FormErrors = {};
  for (const f of fields) {
    const v = values[f.name];
    if (f.required && v.trim() === "") {
      errors[f.name] = `${f.label} is required`;
    }
    if (f.type === "number" && Number.isNaN(Number(v))) {
      errors[f.name] = `${f.label} must be a number`;
    }
    if (f.type === "select" && !f.options.includes(v)) {
      errors[f.name] = `${f.label} must be one of ${f.options.join(", ")}`;
    }
  }
  return errors;
}

export function firstError(errors: FormErrors): string | undefined {
  const keys = Object.keys(errors);
  return errors[keys[0]];
}

export function defaults(fields: Field[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of fields) {
    out[f.name] = f.type === "select" ? f.options[0] : "";
  }
  return out;
}
