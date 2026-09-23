"""Immutable oracle for borealis-north:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-borealis-north', service.scope_owner()


if __name__ == "__main__":
    main()
