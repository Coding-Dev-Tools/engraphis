"""Immutable oracle for borealis-green:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-borealis-green', service.scope_owner()


if __name__ == "__main__":
    main()
