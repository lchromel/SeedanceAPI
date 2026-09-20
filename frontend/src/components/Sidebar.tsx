import type { Asset, Bootstrap, Config, Kind } from "../types";
import { Button, Icon, IconButton } from "./UI";
export function Sidebar({
  collapsed,
  onCollapse,
  config,
  assets,
  onLibrary,
  onChange,
  accountOpen,
  onAccount,
  user,
  onHistory,
  onLogout,
}: {
  collapsed: boolean;
  onCollapse: () => void;
  config: Config;
  assets: Asset[];
  onLibrary: (kind: Kind) => void;
  onChange: (config: Config) => void;
  accountOpen: boolean;
  onAccount: () => void;
  user: Bootstrap;
  onHistory: () => void;
  onLogout: () => void;
}) {
  const asset = (id: string | null) => assets.find((a) => a.id === id);
  return (
    <aside className={`sidebar ${collapsed ? "collapsed" : ""}`}>
      <header>
        {!collapsed && <h1>Look</h1>}
        <IconButton
          icon={collapsed ? "Menu" : "MenuCollapse"}
          label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          onClick={onCollapse}
        />
      </header>
      {collapsed ? (
        <nav aria-label="Look libraries">
          {(["character", "clothing", "location"] as Kind[]).map((k, i) => (
            <IconButton
              key={k}
              icon={["Person", "Shirt", "Location"][i]}
              label={k + " library"}
              onClick={() => onLibrary(k)}
            />
          ))}
        </nav>
      ) : (
        <div className="look-content">
          {(["character", "clothing", "location"] as Kind[]).map((kind) =>
            kind === "clothing" ? (
              <section className="wardrobe" key={kind}>
                <header>
                  <h2>Clothing</h2>
                  <button
                    className="text-button"
                    onClick={() => onLibrary(kind)}
                  >
                    Library <Icon name="ArrowUpRight" size={16} />
                  </button>
                </header>
                <div className="thumbnails">
                  <button
                    className="add-thumb"
                    aria-label="Add clothing"
                    onClick={() => onLibrary(kind)}
                  >
                    <Icon name="Plus" />
                  </button>
                  {config.clothing.map((id) => {
                    const a = asset(id);
                    return (
                      a && (
                        <div className="thumbnail" key={id}>
                          <img src={a.url} alt={a.name} />
                          <button
                            aria-label={`Remove ${a.name}`}
                            onClick={() =>
                              onChange({
                                ...config,
                                clothing: config.clothing.filter(
                                  (x) => x !== id,
                                ),
                              })
                            }
                          >
                            <Icon name="Close" size={14} />
                          </button>
                        </div>
                      )
                    );
                  })}
                </div>
              </section>
            ) : (
              <button
                className={`look-card ${kind}`}
                key={kind}
                onClick={() => onLibrary(kind)}
              >
                {asset(config[kind as "character" | "location"]) ? (
                  <img
                    src={asset(config[kind as "character" | "location"])!.url}
                    alt=""
                  />
                ) : (
                  <div className="look-empty">
                    <Icon
                      name={kind === "character" ? "Person" : "Location"}
                      size={32}
                    />
                    <span>Choose {kind}</span>
                  </div>
                )}
                <footer>
                  <span>
                    {asset(config[kind as "character" | "location"])?.name ||
                      `${kind === "character" ? "Character" : "Location"} library`}
                  </span>
                  <Icon name="Chevron" />
                </footer>
              </button>
            ),
          )}
        </div>
      )}
      <div className="account-anchor">
        <button
          className="account-trigger"
          onClick={onAccount}
          aria-expanded={accountOpen}
          aria-label="Account menu"
        >
          {user.user.name.slice(0, 2).toUpperCase()}
        </button>
        {!collapsed && <span className="account-name">{user.user.name}</span>}
        {accountOpen && (
          <div className="account-menu">
            <header>
              <strong>{user.user.name}</strong>
              <small>Team workspace</small>
            </header>
            <section className="credits">
              <span>Available credits</span>
              <strong>{user.wallet.available.toLocaleString()}</strong>
              <div className="credit-details">
                <span>{user.wallet.held} reserved</span>
                <span>{user.wallet.spent} used</span>
              </div>
            </section>
            <Button onClick={onHistory}>
              Usage history <Icon name="Chevron" size={16} />
            </Button>
            <Button onClick={onLogout}>Sign out</Button>
          </div>
        )}
      </div>
    </aside>
  );
}
