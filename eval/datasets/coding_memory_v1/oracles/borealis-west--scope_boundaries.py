"""Immutable oracle for borealis-west:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-borealis-west', service.scope_owner()


if __name__ == "__main__":
    main()
