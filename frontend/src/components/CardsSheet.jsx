import { useState } from 'react';
import { BadgeCheck, Loader2, Plus, Trash2 } from 'lucide-react';
import { useInstruments } from '../hooks/useInstruments';
import { Sheet } from './ui/Sheet';
import './CardsSheet.css';

const KINDS = [
  ['credit', 'Crédito'],
  ['debit', 'Débito'],
  ['prepaid', 'Prepaga'],
  ['account_balance', 'Dinero en cuenta'],
];

const RAILS = [
  ['card', 'Tarjeta'],
  ['modo', 'MODO'],
  ['mercado_pago', 'Mercado Pago'],
  ['cuenta_dni', 'Cuenta DNI'],
];

const BIN_SOURCE_LABEL = {
  catalog: 'BIN de catálogo',
  user: 'BIN cargado por vos',
  verified: 'BIN verificado',
};

const EMPTY_FORM = {
  label: '',
  issuer_slug: '',
  brand: '',
  kind: 'credit',
  rails: ['card'],
  bins: '',
};

/**
 * Mis tarjetas y billeteras.
 *
 * Nunca se pide el número de tarjeta. El BIN son los primeros 6 a 8 dígitos,
 * identifica al emisor y no a la persona, y es lo único que la simulación
 * necesita para saber si una promo aplica — no hay campo para el número
 * completo, ni para vencimiento, ni para titular, porque ninguno hace falta.
 */
export function CardsSheet({ onClose, onChanged }) {
  const cards = useInstruments(true);
  const [form, setForm] = useState(EMPTY_FORM);
  const [adding, setAdding] = useState(false);
  const [formError, setFormError] = useState(null);
  const [saving, setSaving] = useState(false);

  const isBalance = form.kind === 'account_balance';

  const submit = async (event) => {
    event.preventDefault();
    setFormError(null);
    setSaving(true);
    try {
      await cards.create({
        label: form.label.trim(),
        issuer_slug: form.issuer_slug.trim(),
        brand: form.brand.trim() || null,
        kind: form.kind,
        rails: form.rails,
        // Dinero en cuenta no pasa por la red de tarjetas: no tiene BIN, y
        // mandarlo igual hace que el backend rechace el alta entera.
        bins: isBalance
          ? []
          : form.bins
              .split(/[\s,]+/)
              .map((bin) => bin.trim())
              .filter(Boolean),
      });
      setForm(EMPTY_FORM);
      setAdding(false);
      onChanged();
    } catch (cause) {
      setFormError(cause.message);
    } finally {
      setSaving(false);
    }
  };

  const toggleRail = (rail) =>
    setForm((prev) => ({
      ...prev,
      rails: prev.rails.includes(rail)
        ? prev.rails.filter((r) => r !== rail)
        : [...prev.rails, rail],
    }));

  return (
    <Sheet
      title="Mis tarjetas"
      onClose={onClose}
      footer={
        adding ? null : (
          <button
            className="btn btn--primary btn--block"
            onClick={() => setAdding(true)}
          >
            <Plus size={18} strokeWidth={2} aria-hidden />
            Agregar medio de pago
          </button>
        )
      }
    >
      {cards.status === 'loading' ? (
        <p className="cards-note">Cargando…</p>
      ) : null}
      {cards.error ? <p className="cards-error">{cards.error}</p> : null}

      {cards.status === 'success' && !cards.instruments.length && !adding ? (
        <p className="cards-note">
          Sin medios de pago cargados, los totales son los de góndola. Cargá una
          tarjeta y la comparación pasa a tener en cuenta sus descuentos.
        </p>
      ) : null}

      <ul className="cards-list">
        {cards.instruments.map((card) => (
          <li className="card" key={card.id}>
            <div className="card-main">
              <p className="card-label">{card.label}</p>
              <p className="card-meta">
                {card.issuer_slug}
                {card.brand ? ` · ${card.brand}` : ''} ·{' '}
                {KINDS.find(([k]) => k === card.kind)?.[1] ?? card.kind}
              </p>
              <div className="card-tags">
                <span className="chip chip--bare">
                  {BIN_SOURCE_LABEL[card.bin_source] ?? card.bin_source}
                </span>
                {card.bins?.length ? (
                  <span className="chip num">{card.bins.join(' · ')}</span>
                ) : null}
                {card.verified_at ? (
                  <span className="chip chip--win">
                    <BadgeCheck size={11} strokeWidth={2.5} aria-hidden />
                    verificada
                  </span>
                ) : null}
              </div>
              {card.verified_promotions?.length ? (
                <p className="card-promos">
                  Activa: {card.verified_promotions.join(', ')}
                </p>
              ) : null}
            </div>

            <div className="card-actions">
              {card.bins?.length ? (
                <button
                  className="btn btn--quiet"
                  onClick={() => cards.verify(card.id)}
                  disabled={cards.busyId === card.id}
                >
                  {cards.busyId === card.id ? (
                    <Loader2 size={15} className="spin" aria-hidden />
                  ) : null}
                  Probar
                </button>
              ) : null}
              <button
                className="btn btn--icon"
                onClick={() => cards.remove(card.id)}
                disabled={cards.busyId === card.id}
                aria-label={`Borrar ${card.label}`}
              >
                <Trash2 size={16} strokeWidth={1.75} aria-hidden />
              </button>
            </div>

            {/* Que no active nada no significa que el BIN esté mal: puede ser
                una tarjeta sin beneficios en esa cadena, o una promo que hoy no
                corre. El mensaje del backend ya lo dice así. */}
            {cards.verification?.id === card.id ? (
              <p
                className={`card-verify${
                  cards.verification.result.verified ? ' card-verify--ok' : ''
                }`}
              >
                {cards.verification.result.message}
              </p>
            ) : null}
          </li>
        ))}
      </ul>

      {adding ? (
        <form className="cards-form" onSubmit={submit}>
          <p className="cards-privacy">
            No pidas ni cargues el número de tarjeta. Con los primeros 6 a 8
            dígitos alcanza: identifican al banco, no a vos.
          </p>

          <div className="field">
            <label className="field-label" htmlFor="card-label">
              Nombre
            </label>
            <input
              id="card-label"
              className="input"
              value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })}
              placeholder="Visa Galicia"
              required
              autoFocus
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="card-issuer">
              Emisor
            </label>
            <input
              id="card-issuer"
              className="input"
              list="issuers"
              value={form.issuer_slug}
              onChange={(e) => setForm({ ...form, issuer_slug: e.target.value })}
              placeholder="galicia"
              required
            />
            <datalist id="issuers">
              {cards.issuers.map((issuer) => (
                <option key={issuer.issuer_slug} value={issuer.issuer_slug}>
                  {issuer.display_name}
                </option>
              ))}
            </datalist>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="card-kind">
              Tipo
            </label>
            <select
              id="card-kind"
              className="select"
              value={form.kind}
              onChange={(e) => setForm({ ...form, kind: e.target.value })}
            >
              {KINDS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <span className="field-label">Por dónde pagás</span>
            <div className="cards-rails">
              {RAILS.map(([value, label]) => (
                <button
                  type="button"
                  key={value}
                  className={`chip cards-rail${
                    form.rails.includes(value) ? ' cards-rail--on' : ''
                  }`}
                  aria-pressed={form.rails.includes(value)}
                  onClick={() => toggleRail(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <p className="field-hint">
              La misma Visa puede tener 25% por MODO y nada por posnet, así que
              el riel cambia qué promos aplican.
            </p>
          </div>

          {!isBalance ? (
            <div className="field">
              <label className="field-label" htmlFor="card-bins">
                BIN (opcional)
              </label>
              <input
                id="card-bins"
                className="input num"
                value={form.bins}
                onChange={(e) => setForm({ ...form, bins: e.target.value })}
                placeholder="507858"
                inputMode="numeric"
              />
              <p className="field-hint">
                Los primeros 6 a 8 dígitos de tu tarjeta. Si lo dejás vacío se
                usan los del catálogo del emisor, que pueden no ser los de tu
                plástico — y esa diferencia cambia el precio.
              </p>
            </div>
          ) : (
            <p className="field-hint">
              El dinero en cuenta no tiene BIN: no pasa por la red de tarjetas.
            </p>
          )}

          {formError ? <p className="field-error">{formError}</p> : null}

          <div className="cards-form-actions">
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => {
                setAdding(false);
                setFormError(null);
              }}
            >
              Cancelar
            </button>
            <button className="btn btn--primary" disabled={saving}>
              {saving ? 'Guardando…' : 'Guardar'}
            </button>
          </div>
        </form>
      ) : null}
    </Sheet>
  );
}
