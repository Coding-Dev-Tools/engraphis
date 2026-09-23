"""Immutable oracle for ember-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-ember-west', service.scope_owner()


if __name__ == "__main__":
    main()
