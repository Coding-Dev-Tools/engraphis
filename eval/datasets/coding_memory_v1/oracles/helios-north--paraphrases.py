"""Immutable oracle for helios-north:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-helios-north', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
