import { useEffect, useRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
export function Icon({ name, size = 22 }: { name: string; size?: number }) {
  return (
    <img
      className="icon"
      src={`${import.meta.env.BASE_URL}icons/${name}.svg`}
      alt=""
      width={size}
      height={size}
    />
  );
}
export function Button({
  children,
  primary = false,
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { primary?: boolean }) {
  return (
    <button
      {...props}
      className={`button ${primary ? "primary" : ""} ${props.className || ""}`}
    >
      {children}
    </button>
  );
}
export function IconButton({
  icon,
  label,
  onClick,
}: {
  icon: string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className="icon-button"
      aria-label={label}
      title={label}
      onClick={onClick}
    >
      <Icon name={icon} />
    </button>
  );
}
export function Modal({
  title,
  onClose,
  children,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return (
    <dialog
      ref={ref}
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="modal-header">
        <h2>{title}</h2>
        <IconButton icon="Close" label="Close dialog" onClick={onClose} />
      </div>
      {children}
    </dialog>
  );
}
