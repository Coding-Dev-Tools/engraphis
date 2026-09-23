"""Immutable oracle for borealis-north:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The ledger path retains 18 days and uses UTC.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()
