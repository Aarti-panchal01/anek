import React, { useEffect, useState } from 'react';
import { Check } from 'lucide-react';

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'icon';
type Size = 'desktop' | 'touch';
type ButtonState = 'idle' | 'loading' | 'success';
type ButtonAttributes = {
  [Key in keyof React.ButtonHTMLAttributes<HTMLButtonElement> as Key extends `on${string}` ? never : Key]: React.ButtonHTMLAttributes<HTMLButtonElement>[Key]
};
type Props = Omit<ButtonAttributes, 'children' | 'type' | 'disabled' | 'className' | 'style'> & {
  variant: Variant;
  minWidth: number;
  onPress: () => void | Promise<void>;
  children: React.ReactNode;
  icon?: React.ReactNode;
  state?: ButtonState;
  size?: Size;
  type?: 'button' | 'submit' | 'reset';
  disabled?: boolean;
  className?: string;
  style?: React.CSSProperties;
};

export function Button({ variant, minWidth, onPress, children, icon, state = 'idle', size = 'desktop', className = '', disabled, ...props }: Props) {
  const [showSuccess, setShowSuccess] = useState(false);
  useEffect(() => {
    if (state !== 'success') return;
    setShowSuccess(true);
    const timer = window.setTimeout(() => setShowSuccess(false), 1500);
    return () => window.clearTimeout(timer);
  }, [state]);
  const busy = state === 'loading';
  return <button {...props} type={props.type || 'button'} className={`ui-button ui-button-${variant} ${className}`} data-size={size} style={{ minWidth, ...props.style }} onClick={onPress} disabled={disabled || busy} aria-busy={busy || undefined}>
    <span className="ui-button-icon" aria-hidden="true">{busy ? <span className="ui-button-spinner"/> : showSuccess ? <Check size={15}/> : icon}</span>
    <span>{children}</span>
  </button>;
}
